from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import stat
import subprocess
import sys
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
                self.stdout = ""

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
            stdout = ""

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
            stdout = ""

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
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _write_python_launcher(path: Path) -> None:
    path.write_text(f"#!{Path(sys.executable).resolve()}\n", encoding="utf-8")
    path.chmod(0o755)


_TEST_REPLACEMENT_PID = os.getpid()
_TEST_REPLACEMENT_BIRTH = service_module.process_birth_identity(_TEST_REPLACEMENT_PID)
_TEST_INTERPRETER_DIGEST = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()


def _refresh_fixture(tmp_path: Path) -> tuple[Path, object, Ledger, dict[tuple[int, str], bool], dict[str, bool]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    _write_python_launcher(executable)
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


def _interrupted_refresh_fixture(tmp_path: Path) -> tuple[Path, object, Path, Path, Path]:
    """Build a fenced v19 ledger paired with its exact stopped v18 unit."""

    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    _write_python_launcher(executable)
    state_dir = repository / ".codex-flow"
    state_dir.mkdir()
    ledger_path = state_dir / "workflow.db"
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
    ledger.arm_harness_refresh_fence()
    ledger.close()
    unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
    legacy = service_module._legacy_supervisor_unit(unit)
    config_home = tmp_path / "config"
    installed_path = install_unit(legacy, config_home=config_home)
    return repository, unit, ledger_path, config_home, installed_path


def _same_topology_refresh_fixture(
    tmp_path: Path,
    schema_version: int,
    *,
    fenced: bool = False,
    authority: bool = True,
    active_child: bool = False,
) -> tuple[Path, object, Path, Path, Path, dict[tuple[int, str], bool], dict[str, bool]]:
    repository = tmp_path / f"repo-v{schema_version}"
    repository.mkdir()
    executable = tmp_path / f"codex-flow-v{schema_version}"
    _write_python_launcher(executable)
    state_dir = repository / ".codex-flow"
    state_dir.mkdir()
    ledger_path = state_dir / "workflow.db"
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
    if active_child:
        ledger.create_run("run")
        ledger.create_milestone("run", "milestone")
        claim = ledger.claim_dispatch("run", "milestone", "executor", 1)
        ledger.enqueue_dispatch(
            claim.dispatch_id,
            backend="sdk_headless",
            capsule_json='{"model":"test","prompt":"bounded"}',
            route_json="{}",
            workspace_path=repository,
            result_contract_sha256="c" * 64,
        )
        ledger._db().execute("UPDATE dispatch_queue SET state = 'running' WHERE dispatch_id = ?", (claim.dispatch_id,))
    if fenced:
        ledger.arm_harness_refresh_fence()
    ledger.close()

    if schema_version in {19, 20}:
        connection = sqlite3.connect(ledger_path)
        connection.execute("DROP TABLE program_outcomes")
        if schema_version == 19:
            connection.execute("DROP TABLE dispatch_terminal_integrity")
        connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (str(schema_version),))
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
            (
                "codex_flow_harness_candidate_retention_v19"
                if schema_version == 19
                else "codex_flow_dispatch_terminal_integrity_v20",
            ),
        )
        connection.commit()
        connection.close()
    elif schema_version != 21:
        raise AssertionError(f"unsupported fixture schema: {schema_version}")
    if not authority:
        connection = sqlite3.connect(ledger_path)
        connection.execute("DELETE FROM harness_authority")
        connection.commit()
        connection.close()

    unit = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY")
    config_home = tmp_path / f"config-v{schema_version}"
    install_unit(unit, config_home=config_home)
    processes = {(500, "old-birth"): True}
    service_state = {"active": True}
    return repository, unit, ledger_path, config_home, executable, processes, service_state


@pytest.mark.parametrize("schema_version", [19, 20, 21])
@pytest.mark.parametrize("initial_fenced", [False, True])
@pytest.mark.parametrize("rotated_profile", [False, True])
def test_refresh_migrates_v19_v20_through_current_harness_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int, initial_fenced: bool, rotated_profile: bool
) -> None:
    repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, schema_version, fenced=initial_fenced
    )
    if rotated_profile:
        old_unit = generate_unit(
            repository, executable=_executable, provider_env_key="OPENAI_API_KEY", profile_sha256="c" * 64
        )
        install_unit(old_unit, config_home=config_home)
        unit = generate_unit(
            repository, executable=_executable, provider_env_key="OPENAI_API_KEY", profile_sha256="e" * 64
        )
    old_bytes = unit_path(config_home=config_home, repository_root=repository).read_bytes()
    events: list[str] = []
    shutdown_paths: list[Path] = []
    installed_units: list[str] = []
    original_init = Ledger.__init__
    original_install = service_module.install_unit

    def traced_init(self: Ledger, path: str | Path, **kwargs: object) -> None:
        if kwargs.get("migrate"):
            assert unit_path(config_home=config_home, repository_root=repository).read_bytes() == old_bytes
            events.append("migrate-open")
        original_init(self, path, **kwargs)

    def traced_install(target: object, **kwargs: object) -> Path:
        events.append("publish")
        installed_units.append(getattr(target, "runtime", "unknown"))
        return original_install(target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Ledger, "__init__", traced_init)
    monkeypatch.setattr(service_module, "install_unit", traced_install)

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        operation = argv[2]
        events.append(operation)
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            service_state["active"] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=repository,
                state_root=repository,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
                version=unit.version,
                owner_nonce_sha256="d" * 64,
            )
            replacement.close()
        return Result(0)

    def shutdown(socket_path: Path, _timeout: float) -> dict[str, object]:
        events.append("shutdown")
        shutdown_paths.append(socket_path)
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    now = [0.0]

    def clock() -> float:
        now[0] += 0.001
        return now[0]

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        profile_sha256=unit.profile_sha256,
        native_compatibility_sha256="d" * 64 if rotated_profile else None,
        environment={"OPENAI_API_KEY": "secret"},
        runner=runner,
        process_is_live=lambda pid, birth: processes.get((pid, birth), False),
        clock=clock,
        sleeper=lambda _delay: None,
        shutdown_sender=shutdown,
    )
    assert result["refreshed"] is True
    assert result["old_epoch"] == 1
    assert result["new_epoch"] == 2
    assert shutdown_paths == [repository / ".codex-flow" / "runtime" / "harness.sock"]
    assert "supervisor.sock" not in {path.name for path in shutdown_paths}
    if schema_version < 21:
        assert events.index("shutdown") < events.index("migrate-open") < events.index("publish")
    assert events.index("publish") < events.index("daemon-reload") < events.index("start")
    assert unit_path(config_home=config_home, repository_root=repository).read_bytes() == unit.text.encode()
    assert installed_units == ["harness"]

    migrated = Ledger(ledger_path)
    try:
        assert migrated.schema_version.value == 21
        authority = migrated.harness_authority()
        assert authority is not None
        assert authority["epoch"] == 2
        assert authority["requested_shutdown"] == 0
        assert (
            migrated._db().execute("SELECT 1 FROM sqlite_master WHERE name = 'supervisor_authority'").fetchone() is None
        )
    finally:
        migrated.close()


def test_v20_active_child_rejects_same_topology_migration_before_shutdown(tmp_path: Path) -> None:
    _repository, unit, ledger_path, config_home, _executable, _processes, _service_state = (
        _same_topology_refresh_fixture(tmp_path, 20, active_child=True)
    )
    calls: list[tuple[str, ...]] = []

    with pytest.raises(ServiceRefreshDeferred, match="active"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=lambda argv, **_: calls.append(argv) or _SystemctlResult(0),
            process_is_live=lambda _pid, _birth: True,
            shutdown_sender=lambda *_args: pytest.fail("active child must reject before shutdown"),
        )

    assert calls == []
    check = sqlite3.connect(ledger_path)
    try:
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "20"
        assert check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 0
    finally:
        check.close()


def test_v20_missing_authority_rejects_same_topology_migration_before_shutdown(tmp_path: Path) -> None:
    _repository, unit, ledger_path, config_home, _executable, _processes, _service_state = (
        _same_topology_refresh_fixture(tmp_path, 20, authority=False)
    )
    calls: list[tuple[str, ...]] = []

    with pytest.raises(ServiceRefreshFailed, match="authority is unavailable"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=lambda argv, **_: calls.append(argv) or _SystemctlResult(0),
            process_is_live=lambda _pid, _birth: True,
            shutdown_sender=lambda *_args: pytest.fail("missing authority must reject before shutdown"),
        )

    assert calls == []
    check = sqlite3.connect(ledger_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM harness_authority").fetchone()[0] == 0
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "20"
    finally:
        check.close()


@pytest.mark.parametrize("failure_stage", ["before_commit", "after_commit"])
def test_v20_migration_failure_keeps_current_topology_forward_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    _repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 20
    )
    old_unit = generate_unit(
        _repository, executable=_executable, provider_env_key="OPENAI_API_KEY", profile_sha256="c" * 64
    )
    install_unit(old_unit, config_home=config_home)
    unit = generate_unit(
        _repository, executable=_executable, provider_env_key="OPENAI_API_KEY", profile_sha256="e" * 64
    )
    original_init = Ledger.__init__
    calls: list[tuple[str, ...]] = []

    if failure_stage == "before_commit":

        def migration_fault(stage: str) -> None:
            if stage == "after_migration":
                raise RuntimeError("injected same-topology migration failure")

        def fail_migration(self: Ledger, path: str | Path, **kwargs: object) -> None:
            if kwargs.get("migrate"):
                kwargs["fault_injector"] = migration_fault
            original_init(self, path, **kwargs)

        monkeypatch.setattr(Ledger, "__init__", fail_migration)
    else:
        original_store = Ledger._ensure_h4_store

        def fail_after_commit(self: Ledger) -> None:
            original_store(self)
            if self._migrate_requested:
                raise RuntimeError("injected post-commit migration opener failure")

        monkeypatch.setattr(Ledger, "_ensure_h4_store", fail_after_commit)

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    with pytest.raises(ServiceRefreshFailed):
        refresh_with_credential(
            unit,
            config_home=config_home,
            profile_sha256=unit.profile_sha256,
            native_compatibility_sha256="d" * 64,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            shutdown_sender=shutdown,
        )

    assert "start" not in [call[2] for call in calls]
    assert (config_home / "systemd" / "user" / unit.unit_name).read_bytes() == old_unit.text.encode("utf-8")
    check = sqlite3.connect(ledger_path)
    try:
        expected_version = "20" if failure_stage == "before_commit" else "21"
        assert (
            check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
            == expected_version
        )
        assert check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 1
        assert check.execute("SELECT 1 FROM sqlite_master WHERE name = 'supervisor_authority'").fetchone() is None
    finally:
        check.close()


def test_v20_committed_migration_failure_retries_forward_without_shutdown_or_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 20
    )
    original_store = Ledger._ensure_h4_store

    def fail_after_commit(self: Ledger) -> None:
        original_store(self)
        if self._migrate_requested:
            raise RuntimeError("injected post-commit migration opener failure")

    monkeypatch.setattr(Ledger, "_ensure_h4_store", fail_after_commit)
    first_calls: list[tuple[str, ...]] = []

    def first_runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        first_calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    with pytest.raises(ServiceRefreshFailed):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=first_runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            shutdown_sender=shutdown,
        )
    monkeypatch.setattr(Ledger, "_ensure_h4_store", original_store)

    retry_calls: list[tuple[str, ...]] = []

    def retry_runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        retry_calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            service_state["active"] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=repository,
                state_root=repository,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
                version=unit.version,
                owner_nonce_sha256="d" * 64,
            )
            replacement.close()
        return _SystemctlResult(0)

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        environment={"OPENAI_API_KEY": "secret"},
        runner=retry_runner,
        process_is_live=lambda pid, birth: processes.get((pid, birth), False),
        sleeper=lambda _delay: pytest.fail("forward retry should not wait"),
        shutdown_sender=lambda *_args: pytest.fail("forward retry must not shut down the stopped predecessor"),
    )
    assert result["refreshed"] is True
    assert "start" in [call[2] for call in retry_calls]
    assert "stop" not in [call[2] for call in retry_calls]


def test_v20_uncertain_start_retains_current_unit_and_fence_without_rollback(tmp_path: Path) -> None:
    _repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 20
    )
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            return _SystemctlResult(1)
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    with pytest.raises(ServiceRefreshFailed, match="start failed"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            shutdown_sender=shutdown,
        )

    assert "stop" not in [call[2] for call in calls]
    assert (config_home / "systemd" / "user" / unit.unit_name).read_bytes() == unit.text.encode("utf-8")
    check = Ledger(ledger_path)
    try:
        assert check.schema_version.value == 21
        authority = check.harness_authority()
        assert authority is not None and authority["requested_shutdown"] == 1
    finally:
        check.close()


@pytest.mark.parametrize("identity_failure", ["interpreter_digest", "proc_executable", "pid_reuse"])
def test_refresh_rejects_replacement_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_failure: str
) -> None:
    """Replacement health requires the installed interpreter and one PID birth."""

    repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    replacement_birth = _TEST_REPLACEMENT_BIRTH if identity_failure != "pid_reuse" else "replacement-birth"
    processes[(_TEST_REPLACEMENT_PID, replacement_birth)] = False
    calls: list[tuple[str, ...]] = []

    if identity_failure == "proc_executable":
        monkeypatch.setattr(service_module, "_process_executable_path", lambda _pid: Path("/bin/sh"))

    identity_values = iter((replacement_birth, "reused-birth"))

    def read_identity(_pid: int) -> str:
        if identity_failure == "pid_reuse":
            return next(identity_values)
        return replacement_birth

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, replacement_birth)] = True
            replacement = Ledger(ledger.path)
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=replacement_birth,
                    executable_digest=(
                        "c" * 64 if identity_failure == "interpreter_digest" else _TEST_INTERPRETER_DIGEST
                    ),
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                replacement.close()
        elif operation == "stop":
            service_state["active"] = False
            processes[(_TEST_REPLACEMENT_PID, replacement_birth)] = False
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    try:
        with pytest.raises(ServiceRefreshFailed, match="health check failed"):
            refresh_with_credential(
                unit,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                ledger=ledger,
                process_is_live=lambda pid, birth: processes.get((pid, birth), False),
                identity_reader=read_identity,
                shutdown_sender=shutdown,
            )
        assert "start" in [call[2] for call in calls]
        assert "stop" in [call[2] for call in calls]
        assert (tmp_path / "config" / "systemd" / "user" / unit.unit_name).read_text(encoding="utf-8") == unit.text
        authority = ledger.harness_authority()
        assert authority is not None and authority["requested_shutdown"] == 1
    finally:
        ledger.close()


def test_refresh_rejects_malformed_launcher_before_fencing(tmp_path: Path) -> None:
    _repository, unit, ledger, _processes, _service_state = _refresh_fixture(tmp_path)
    unit.executable.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    try:
        with pytest.raises(ServiceRefreshFailed, match="direct absolute Python shebang"):
            refresh_with_credential(
                unit,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=lambda argv, **_: (
                    calls.append(argv)
                    or (
                        _SystemctlResult(0, "ActiveState=inactive\nMainPID=0\n")
                        if argv[2] == "show"
                        else _SystemctlResult(3)
                    )
                ),
                ledger=ledger,
                process_is_live=lambda _pid, _birth: True,
                shutdown_sender=lambda *_args: pytest.fail("malformed launcher must fail before shutdown"),
            )
        assert calls == []
        authority = ledger.harness_authority()
        assert authority is not None and authority["requested_shutdown"] == 0
    finally:
        ledger.close()


def test_refresh_rejects_launcher_drift_after_capture(tmp_path: Path) -> None:
    repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            unit.executable.write_text(f"#!{Path(sys.executable).resolve()}\n# launcher drift\n", encoding="utf-8")
            replacement = Ledger(ledger.path)
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                replacement.close()
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    try:
        with pytest.raises(ServiceRefreshFailed, match="launcher changed during replacement health"):
            refresh_with_credential(
                unit,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                ledger=ledger,
                process_is_live=lambda pid, birth: processes.get((pid, birth), False),
                identity_reader=lambda _pid: _TEST_REPLACEMENT_BIRTH,
                shutdown_sender=shutdown,
            )
        assert "start" in [call[2] for call in calls]
        assert "stop" not in [call[2] for call in calls]
        authority = ledger.harness_authority()
        assert authority is not None and authority["requested_shutdown"] == 0
    finally:
        ledger.close()


def test_refresh_rejects_predecessor_authority_drift_before_migration(tmp_path: Path) -> None:
    _repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 20
    )

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        if operation == "show":
            return _SystemctlResult(0, "ActiveState=inactive\nMainPID=0\n")
        return _SystemctlResult(0 if operation == "is-active" and service_state["active"] else 3)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        connection = sqlite3.connect(ledger_path)
        connection.execute("UPDATE harness_authority SET pid = 501 WHERE singleton = 1")
        connection.commit()
        connection.close()
        return {"version": 1, "ok": True, "operation": "shutdown"}

    with pytest.raises(ServiceRefreshFailed, match="authority changed"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            shutdown_sender=shutdown,
        )

    check = sqlite3.connect(ledger_path)
    try:
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "20"
        assert check.execute("SELECT pid FROM harness_authority WHERE singleton = 1").fetchone()[0] == 501
        assert check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 1
    finally:
        check.close()


def test_refresh_rejects_authority_drift_between_legacy_close_and_migration_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 19
    )
    original_init = Ledger.__init__
    raced = False

    def race_migration_open(self: Ledger, path: str | Path, **kwargs: object) -> None:
        nonlocal raced
        if kwargs.get("migrate") and not raced:
            raced = True
            connection = sqlite3.connect(ledger_path)
            connection.execute("UPDATE harness_authority SET pid = 501 WHERE singleton = 1")
            connection.commit()
            connection.close()
        original_init(self, path, **kwargs)

    monkeypatch.setattr(Ledger, "__init__", race_migration_open)

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        if operation == "show":
            return _SystemctlResult(0, "ActiveState=inactive\nMainPID=0\n")
        return _SystemctlResult(0 if operation == "is-active" and service_state["active"] else 3)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    with pytest.raises(ServiceRefreshFailed, match="ledger operation failed"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            shutdown_sender=shutdown,
        )

    assert raced is True
    check = sqlite3.connect(ledger_path)
    try:
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "19"
        assert check.execute("SELECT pid FROM harness_authority WHERE singleton = 1").fetchone()[0] == 501
        assert check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 1
    finally:
        check.close()


@pytest.mark.parametrize("socket_name", ["harness.sock", "supervisor.sock"])
@pytest.mark.parametrize("entry_kind", ["regular", "dangling", "socket"])
def test_current_schema_reentry_rejects_any_retained_socket_entry(
    tmp_path: Path, socket_name: str, entry_kind: str
) -> None:
    repository, unit, ledger_path, config_home, _executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 21, fenced=True
    )
    install_unit(service_module._legacy_supervisor_unit(unit), config_home=config_home)
    processes[(500, "old-birth")] = False
    service_state["active"] = False
    runtime = repository / ".codex-flow" / "runtime"
    runtime.mkdir(exist_ok=True)
    socket_path = runtime / socket_name
    bound_socket: socket.socket | None = None
    bound_target: Path | None = None
    if entry_kind == "regular":
        socket_path.write_text("retained", encoding="utf-8")
    elif entry_kind == "dangling":
        socket_path.symlink_to(runtime / "missing.sock")
    else:
        bound_target = Path("/tmp") / f"codex-flow-{os.getpid()}-{abs(hash(os.fspath(socket_path)))}"
        bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_socket.bind(os.fspath(bound_target))
        socket_path.symlink_to(bound_target)

    calls: list[tuple[str, ...]] = []
    try:
        with pytest.raises(ServiceRefreshFailed, match="socket entry remains"):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=lambda argv, **_: (
                    calls.append(argv)
                    or (
                        _SystemctlResult(0, "ActiveState=inactive\nMainPID=0\n")
                        if argv[2] == "show"
                        else _SystemctlResult(3)
                    )
                ),
                process_is_live=lambda pid, birth: processes.get((pid, birth), False),
            )
        assert "start" not in [call[2] for call in calls]
        check = sqlite3.connect(ledger_path)
        try:
            assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "21"
            assert (
                check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 1
            )
        finally:
            check.close()
    finally:
        if bound_socket is not None:
            bound_socket.close()
        if bound_target is not None:
            bound_target.unlink(missing_ok=True)


def test_current_unhealthy_replacement_retains_current_unit_and_fresh_retry_succeeds(tmp_path: Path) -> None:
    repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    first_birth = "first-replacement"
    processes[(_TEST_REPLACEMENT_PID, first_birth)] = False
    first_calls: list[tuple[str, ...]] = []

    def first_runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        first_calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, first_birth)] = True
            replacement = Ledger(ledger.path)
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=first_birth,
                    executable_digest="c" * 64,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                replacement.close()
        elif operation == "stop":
            service_state["active"] = False
            processes[(_TEST_REPLACEMENT_PID, first_birth)] = False
        return _SystemctlResult(0)

    def shutdown(_socket_path: Path, _timeout: float) -> dict[str, object]:
        processes[(500, "old-birth")] = False
        service_state["active"] = False
        return {"version": 1, "ok": True, "operation": "shutdown"}

    try:
        with pytest.raises(ServiceRefreshFailed, match="health check failed"):
            refresh_with_credential(
                unit,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=first_runner,
                ledger=ledger,
                process_is_live=lambda pid, birth: processes.get((pid, birth), False),
                identity_reader=lambda _pid: first_birth,
                shutdown_sender=shutdown,
            )
        assert "stop" in [call[2] for call in first_calls]
        assert (tmp_path / "config" / "systemd" / "user" / unit.unit_name).read_text(encoding="utf-8") == unit.text
        authority = ledger.harness_authority()
        assert authority is not None and authority["epoch"] == 2 and authority["requested_shutdown"] == 1
    finally:
        ledger.close()

    retry_birth = "retry-replacement"
    processes[(_TEST_REPLACEMENT_PID, retry_birth)] = False
    retry_started = False

    def retry_runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        nonlocal retry_started
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            retry_started = True
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, retry_birth)] = True
            replacement = Ledger(repository / ".codex-flow" / "workflow.db")
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=retry_birth,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="e" * 64,
                )
            finally:
                replacement.close()
        return _SystemctlResult(0)

    result = refresh_with_credential(
        unit,
        config_home=tmp_path / "config",
        environment={"OPENAI_API_KEY": "secret"},
        runner=retry_runner,
        process_is_live=lambda pid, birth: processes.get((pid, birth), False),
        identity_reader=lambda _pid: retry_birth,
        sleeper=lambda _delay: pytest.fail("current retry should not wait"),
        shutdown_sender=lambda *_args: pytest.fail("retry must not shut down the stopped replacement"),
    )
    assert result["refreshed"] is True
    assert result["old_epoch"] == 2 and result["new_epoch"] == 3
    assert retry_started is True


@pytest.mark.parametrize("failure_stage", ["daemon-reload", "import-environment", "start", "health"])
def test_interrupted_v19_refresh_classifies_each_post_staging_failure_without_hiding_replacement(
    tmp_path: Path, failure_stage: str
) -> None:
    _repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    before_bytes = installed_path.read_bytes()
    before_mode = stat.S_IMODE(installed_path.stat().st_mode)
    calls: list[tuple[str, ...]] = []
    now = [0.0]

    def clock() -> float:
        now[0] += 0.1
        return now[0]

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
            return result
        if operation == "is-active":
            return _SystemctlResult(3)
        if operation == failure_stage:
            return _SystemctlResult(1)
        return _SystemctlResult(0)

    with pytest.raises(ServiceRefreshFailed):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda _pid, _birth: False,
            clock=clock,
            sleeper=lambda _delay: None,
            deadline_seconds=0.2,
            shutdown_sender=lambda *_args: pytest.fail("stopped predecessor must not be shut down"),
        )

    if failure_stage in {"start", "health"}:
        # Once start was invoked, or its outcome became possible, the current
        # harness bytes remain installed even when no replacement authority is
        # observable.  Direct restoration would hide an uncertain process.
        assert installed_path.read_text(encoding="utf-8") == unit.text
    else:
        assert installed_path.read_bytes() == before_bytes
    assert stat.S_IMODE(installed_path.stat().st_mode) == before_mode == 0o600
    metadata = installed_path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    assert [call[2] for call in calls][:1] == ["show"]
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "21"
        authority = connection.execute(
            "SELECT requested_shutdown FROM harness_authority WHERE singleton = 1"
        ).fetchone()
        assert authority is not None and authority[0] == 1
    finally:
        connection.close()

    replacement_started = False

    def retry_runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        nonlocal replacement_started
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if replacement_started else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if replacement_started else 3)
        if operation == "start":
            replacement_started = True
            replacement = Ledger(ledger_path)
            try:
                replacement.acquire_harness(
                    repository_root=unit.repository_root,
                    state_root=unit.state_root,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                replacement.close()
        return _SystemctlResult(0)

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        environment={"OPENAI_API_KEY": "secret"},
        runner=retry_runner,
        process_is_live=lambda pid, birth: (
            pid == _TEST_REPLACEMENT_PID and birth == _TEST_REPLACEMENT_BIRTH and replacement_started
        ),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
        shutdown_sender=lambda *_args: pytest.fail("stopped predecessor must not be shut down again"),
    )
    assert result["refreshed"] is True


def test_interrupted_v19_refresh_rejects_arbitrary_legacy_unit_drift_before_manager_mutation(
    tmp_path: Path,
) -> None:
    _repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    installed_path.write_bytes(installed_path.read_bytes() + b"# unexpected drift\n")
    calls: list[tuple[str, ...]] = []

    with pytest.raises(ServiceError, match="service unit content drifted"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=lambda argv, **_: (
                calls.append(argv)
                or (
                    _SystemctlResult(0, "ActiveState=inactive\nMainPID=0\n")
                    if argv[2] == "show"
                    else _SystemctlResult(3)
                )
            ),
            process_is_live=lambda _pid, _birth: False,
            shutdown_sender=lambda *_args: pytest.fail("unit drift must fail before shutdown"),
        )

    assert calls == []
    connection = sqlite3.connect(ledger_path)
    try:
        authority = connection.execute(
            "SELECT requested_shutdown FROM harness_authority WHERE singleton = 1"
        ).fetchone()
        assert authority is not None and authority[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "classification",
    ["malformed_shutdown", "missing_authority", "ambiguous_authority", "identity_drift", "uncertain_start"],
)
def test_interrupted_v19_refresh_keeps_harness_bytes_for_post_start_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, classification: str
) -> None:
    _repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    started = False
    service_active = False
    current_ledger = Ledger(ledger_path)
    original_authority = current_ledger.harness_authority

    def observed_authority() -> object:
        if not started:
            return original_authority()
        if classification == "missing_authority":
            return None
        if classification == "ambiguous_authority":
            return []
        authority = original_authority()
        assert isinstance(authority, dict)
        if classification == "malformed_shutdown":
            return {
                **authority,
                "pid": 501,
                "process_birth_identity": _TEST_REPLACEMENT_BIRTH,
                "epoch": int(authority["epoch"]) + 1,
                "requested_shutdown": 2,
            }
        if classification == "identity_drift":
            return {**authority, "repository_root": "/different-repository", "epoch": int(authority["epoch"]) + 1}
        return authority

    monkeypatch.setattr(current_ledger, "harness_authority", observed_authority)

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        nonlocal started, service_active
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = "ActiveState=active\nMainPID=1\n" if service_active else "ActiveState=inactive\nMainPID=0\n"
            return result
        if operation == "is-active":
            return Result(0 if service_active else 3)
        if operation == "start":
            started = True
            service_active = True
            return Result(1 if classification == "uncertain_start" else 0)
        return Result(0)

    try:
        with pytest.raises(ServiceRefreshFailed):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                ledger=current_ledger,
                process_is_live=lambda pid, birth: (
                    started and pid == _TEST_REPLACEMENT_PID and birth == _TEST_REPLACEMENT_BIRTH
                ),
                clock=lambda: 0.0,
                sleeper=lambda _delay: pytest.fail("uncertain refresh must fail without polling"),
                shutdown_sender=lambda *_args: pytest.fail("interrupted predecessor must not be shut down again"),
            )
    finally:
        current_ledger.close()

    assert installed_path.read_text(encoding="utf-8") == unit.text
    assert "stop" not in [call[2] for call in calls]


def test_started_replacement_health_failure_is_refenced_stopped_and_restored_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    before_bytes = installed_path.read_bytes()
    before_mode = stat.S_IMODE(installed_path.stat().st_mode)
    replacement = {"started": False}
    process_live = {(500, "old-birth"): False, (_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH): False}
    service_active = {"value": False}
    calls: list[tuple[str, ...]] = []
    fence_calls = 0
    original_fence = Ledger.arm_harness_refresh_fence

    def count_fence(self: Ledger) -> object:
        nonlocal fence_calls
        fence_calls += 1
        return original_fence(self)

    monkeypatch.setattr(Ledger, "arm_harness_refresh_fence", count_fence)

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_active["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if service_active["value"] else 3)
        if operation == "start":
            replacement["started"] = True
            service_active["value"] = True
            acquired = Ledger(ledger_path)
            try:
                acquired.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                acquired.close()
        elif operation == "stop":
            service_active["value"] = False
            process_live[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = False
        return Result(0)

    current_ledger = Ledger(ledger_path)
    try:
        with pytest.raises(ServiceRefreshFailed, match="health check failed"):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda pid, birth: process_live.get((pid, birth), False),
                clock=lambda: 0.0,
                sleeper=lambda _delay: pytest.fail("replacement rollback should complete without waiting"),
                shutdown_sender=lambda *_args: pytest.fail("fenced predecessor must not be shut down again"),
                ledger=current_ledger,
            )
    finally:
        current_ledger.close()

    assert replacement["started"] is True
    assert [call[2] for call in calls] == [
        "show",
        "is-active",
        "show",
        "daemon-reload",
        "import-environment",
        "show",
        "start",
        "is-active",
        "stop",
        "is-active",
        "show",
        "unset-environment",
    ]
    assert fence_calls == 1
    assert installed_path.read_bytes() == before_bytes
    assert stat.S_IMODE(installed_path.stat().st_mode) == before_mode == 0o600
    check = Ledger(ledger_path)
    try:
        authority = check.harness_authority()
        assert authority is not None
        assert authority["epoch"] == 2
        assert authority["pid"] == _TEST_REPLACEMENT_PID
        assert authority["process_birth_identity"] == _TEST_REPLACEMENT_BIRTH
        assert authority["requested_shutdown"] == 1
    finally:
        check.close()

    retry_started = {"value": False}

    def retry_runner(argv: tuple[str, ...], **_: object) -> Result:
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if retry_started["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if retry_started["value"] else 3)
        if operation == "start":
            retry_started["value"] = True
            acquired = Ledger(ledger_path)
            try:
                acquired.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity="retry-birth",
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="f" * 64,
                )
            finally:
                acquired.close()
        return Result(0)

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        environment={"OPENAI_API_KEY": "secret"},
        runner=retry_runner,
        process_is_live=lambda pid, birth: (
            pid == _TEST_REPLACEMENT_PID and birth == "retry-birth" and retry_started["value"]
        ),
        identity_reader=lambda _pid: "retry-birth",
        clock=lambda: 0.0,
        sleeper=lambda _delay: pytest.fail("canonical retry should complete without waiting"),
        shutdown_sender=lambda *_args: pytest.fail("stopped replacement must not be shut down again"),
    )
    assert result["refreshed"] is True
    assert result["old_epoch"] == 2
    assert result["new_epoch"] == 3


@pytest.mark.parametrize("requested_shutdown", (0, 1))
def test_refresh_rolls_back_matching_higher_epoch_with_existing_or_new_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested_shutdown: int
) -> None:
    """Both replacement fence states use one exact stop/death/revalidation path."""

    repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    before_bytes = installed_path.read_bytes()
    process_live = {(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH): requested_shutdown == 1}
    service_active = {"value": False}
    calls: list[tuple[str, ...]] = []
    fence_calls = 0
    original_fence = Ledger.arm_harness_refresh_fence

    def count_fence(self: Ledger) -> object:
        nonlocal fence_calls
        fence_calls += 1
        return original_fence(self)

    monkeypatch.setattr(Ledger, "arm_harness_refresh_fence", count_fence)

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_active["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if service_active["value"] else 3)
        if operation == "start":
            replacement = Ledger(ledger_path)
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
                if requested_shutdown == 1:
                    replacement.request_harness_shutdown(epoch=2, owner_nonce_sha256="d" * 64)
            finally:
                replacement.close()
            service_active["value"] = True
        elif operation == "stop":
            service_active["value"] = False
            process_live[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = False
        return Result(0)

    ledger = Ledger(ledger_path)
    try:
        with pytest.raises(ServiceRefreshFailed, match="health check failed"):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda pid, birth: process_live.get((pid, birth), False),
                clock=lambda: 0.0,
                sleeper=lambda _delay: pytest.fail("replacement rollback should not poll after stop"),
                shutdown_sender=lambda *_args: pytest.fail("interrupted predecessor must not be shut down again"),
                ledger=ledger,
            )
    finally:
        ledger.close()

    assert installed_path.read_bytes() == before_bytes
    assert [call[2] for call in calls] == (
        [
            "show",
            "is-active",
            "show",
            "daemon-reload",
            "import-environment",
            "show",
            "start",
            "is-active",
            "stop",
            "is-active",
            "show",
            "unset-environment",
        ]
        if requested_shutdown == 0
        else [
            "show",
            "is-active",
            "show",
            "daemon-reload",
            "import-environment",
            "show",
            "start",
            "stop",
            "is-active",
            "show",
            "unset-environment",
        ]
    )
    assert fence_calls == (1 if requested_shutdown == 0 else 0)


def test_refresh_positive_predecessor_absence_proof_restores_only_after_three_spaced_observations(
    tmp_path: Path,
) -> None:
    _repository, unit, _ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    before_bytes = installed_path.read_bytes()
    now = [0.0]
    sleeps: list[float] = []

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
            return result
        if operation == "is-active":
            return Result(3)
        if operation == "start":
            return Result(0)
        return Result(0)

    def clock() -> float:
        return now[0]

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    ledger = Ledger(_ledger_path)
    try:
        with pytest.raises(ServiceRefreshFailed, match="did not acquire a newer epoch"):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda _pid, _birth: False,
                clock=clock,
                sleeper=sleeper,
                deadline_seconds=1.0,
                shutdown_sender=lambda *_args: pytest.fail("interrupted predecessor must not be shut down again"),
                ledger=ledger,
            )
    finally:
        ledger.close()

    assert installed_path.read_bytes() == before_bytes
    assert len(sleeps) == 2
    assert all(delay >= 0.05 for delay in sleeps)


def test_refresh_accepts_legitimate_replacement_after_predecessor_wait_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delayed greater epoch is not drift from the fenced predecessor row."""

    _repository, unit, ledger_path, config_home, _installed_path = _interrupted_refresh_fixture(tmp_path)
    ledger = Ledger(ledger_path)
    predecessor = ledger.harness_authority()
    assert predecessor is not None
    replacement = {
        **predecessor,
        "pid": _TEST_REPLACEMENT_PID,
        "process_birth_identity": _TEST_REPLACEMENT_BIRTH,
        "epoch": int(predecessor["epoch"]) + 1,
        "requested_shutdown": 0,
        "executable_digest": _TEST_INTERPRETER_DIGEST,
    }
    # The first read is the interrupted-recovery preflight.  The next three
    # predecessor reads exercise repeated post-start wait-loop observations
    # before acquisition.
    observations = iter((predecessor, predecessor, predecessor, predecessor, replacement))
    observed: list[object] = []

    def observed_authority() -> object:
        value = next(observations, replacement)
        observed.append(value)
        return value

    monkeypatch.setattr(ledger, "harness_authority", observed_authority)
    service_active = {"value": False}
    sleeps: list[float] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_active["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_active["value"] else 3)
        if operation == "start":
            service_active["value"] = True
        return _SystemctlResult(0)

    try:
        result = refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            ledger=ledger,
            process_is_live=lambda pid, birth: (pid, birth) == (_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH),
            clock=lambda: 0.0,
            sleeper=lambda delay: sleeps.append(delay),
            shutdown_sender=lambda *_args: pytest.fail("interrupted predecessor must not be shut down again"),
        )
    finally:
        ledger.close()

    assert result["refreshed"] is True
    assert result["old_epoch"] == int(predecessor["epoch"])
    assert result["new_epoch"] == int(replacement["epoch"])
    assert observed[:5] == [predecessor, predecessor, predecessor, predecessor, replacement]
    assert sleeps == [0.05, 0.05]


def test_refresh_rejects_replacement_to_different_replacement_identity_drift(tmp_path: Path) -> None:
    """Once a replacement is acquired, a different replacement cannot win rollback."""

    repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    ledger = Ledger(ledger_path)
    predecessor = ledger.harness_authority()
    assert predecessor is not None
    service_active = {"value": False}

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_active["value"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_active["value"] else 3)
        if operation == "start":
            service_active["value"] = True
            acquired = Ledger(ledger_path)
            try:
                acquired.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity="replacement-a",
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                acquired.close()
        elif operation == "stop":
            service_active["value"] = False
            drifted = Ledger(ledger_path)
            try:
                replacement = drifted.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=502,
                    process_birth_identity="replacement-b",
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
                drifted.request_harness_shutdown(
                    epoch=int(replacement["epoch"]),
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                drifted.close()
        return _SystemctlResult(0)

    try:
        with pytest.raises(ServiceRefreshFailed, match="replacement harness authority changed after stop"):
            refresh_with_credential(
                unit,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                ledger=ledger,
                process_is_live=lambda _pid, _birth: False,
                clock=lambda: 0.0,
                sleeper=lambda _delay: pytest.fail("replacement rollback should not wait"),
                shutdown_sender=lambda *_args: pytest.fail("interrupted predecessor must not be shut down again"),
            )
    finally:
        ledger.close()

    assert installed_path.read_text(encoding="utf-8") == unit.text


@pytest.mark.parametrize(
    "stopped_response",
    [
        "ActiveState=inactive\nMainPID=0\n",
        "MainPID=0\nActiveState=inactive\n",
        "ActiveState=inactive\nMainPID=0",
        "MainPID=0\nActiveState=inactive",
    ],
)
def test_refresh_handoff_fences_shutdowns_by_identity_and_verifies_replacement(
    tmp_path: Path, stopped_response: str
) -> None:
    _repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    shutdowns: list[tuple[Path, float]] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = _SystemctlResult(0)
            result.stdout = "ActiveState=active\nMainPID=1\n" if service_state["active"] else stopped_response
            return result
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            service_state["active"] = True
            ledger.acquire_harness(
                repository_root=unit.repository_root,
                state_root=unit.state_root,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
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
            "show",
            "show",
            "daemon-reload",
            "import-environment",
            "show",
            "start",
            "is-active",
        ]
        assert all("restart" not in call for call in calls)
        authority = ledger.harness_authority()
        assert authority is not None
        assert authority["requested_shutdown"] == 0
        assert authority["process_birth_identity"] == _TEST_REPLACEMENT_BIRTH
    finally:
        ledger.close()


def test_refresh_defers_before_shutdown_or_manager_mutation_for_active_child(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    _write_python_launcher(executable)
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
    _write_python_launcher(executable)
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
        if argv[2] == "show":
            result = _SystemctlResult(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
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
    _write_python_launcher(executable)
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
    connection.execute("DROP TABLE dispatch_terminal_integrity")
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
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if service_state["active"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if service_state["active"] else 3)
        if operation == "start":
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            service_state["active"] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=repository,
                state_root=repository,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
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
    assert [call[2] for call in calls] == [
        "is-active",
        "show",
        "daemon-reload",
        "import-environment",
        "show",
        "start",
        "is-active",
    ]
    migrated = Ledger(ledger_path)
    try:
        assert migrated.schema_version.value == 21
        assert (
            migrated._db().execute("SELECT 1 FROM sqlite_master WHERE name = 'supervisor_authority'").fetchone() is None
        )
    finally:
        migrated.close()


def test_v18_refresh_install_failure_keeps_legacy_pair_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    _write_python_launcher(executable)
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
    connection.execute("DROP TABLE dispatch_terminal_integrity")
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
    installed_path = install_unit(legacy, config_home=config_home)
    before = installed_path.read_bytes()
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 0
        stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        if argv[2] == "show":
            result = Result()
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
            return result
        if argv[2] == "is-active":
            return type("InactiveResult", (), {"returncode": 3, "stdout": ""})()
        return Result()

    def fail_install(*_args: object, **_kwargs: object) -> Path:
        raise ServiceError("staged unit install failed")

    monkeypatch.setattr(service_module, "install_unit", fail_install)
    with pytest.raises(ServiceError, match="staged unit install failed"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda _pid, _birth: False,
            shutdown_sender=lambda *_args: {"version": 1, "ok": True, "operation": "shutdown"},
        )
    assert installed_path.read_bytes() == before
    check = sqlite3.connect(ledger_path)
    try:
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "18"
        assert (
            check.execute("SELECT requested_shutdown FROM supervisor_authority WHERE singleton = 1").fetchone()[0] == 1
        )
    finally:
        check.close()
    assert [call[2] for call in calls] == ["is-active", "show"]


@pytest.mark.parametrize("failure_stage", ["before_commit", "after_commit", "rollback_failure"])
def test_v18_refresh_migration_opener_failure_restores_legacy_pair_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    _write_python_launcher(executable)
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
    connection.execute("DROP TABLE dispatch_terminal_integrity")
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
    installed_path = install_unit(legacy, config_home=config_home)
    before_bytes = installed_path.read_bytes()
    before_mode = stat.S_IMODE(installed_path.stat().st_mode)
    calls: list[tuple[str, ...]] = []

    class Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        if argv[2] == "show":
            result = Result(0)
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
            return result
        if argv[2] == "is-active":
            return Result(3)
        return Result(0)

    if failure_stage == "before_commit":
        original_init = Ledger.__init__

        def migration_fault(stage: str) -> None:
            if stage == "after_migration":
                raise RuntimeError("injected pre-commit migration opener failure")

        def fail_migration(self: Ledger, path: str | Path, **kwargs: object) -> None:
            if kwargs.get("migrate"):
                kwargs["fault_injector"] = migration_fault
            original_init(self, path, **kwargs)

        monkeypatch.setattr(Ledger, "__init__", fail_migration)
    else:
        ensure_store_before_fault = Ledger._ensure_h4_store

        def fail_after_committed_migration(self: Ledger) -> None:
            ensure_store_before_fault(self)
            if self._migrate_requested:
                raise RuntimeError("injected post-commit migration opener failure")

        monkeypatch.setattr(Ledger, "_ensure_h4_store", fail_after_committed_migration)
        if failure_stage == "rollback_failure":

            def fail_compensation(self: Ledger) -> None:
                raise RuntimeError("injected compensation failure")

            monkeypatch.setattr(Ledger, "_restore_fenced_v18_after_failed_open", fail_compensation)
    failure_match = "did not restore schema v18" if failure_stage == "rollback_failure" else "harness refresh failed"
    with pytest.raises(ServiceRefreshFailed, match=failure_match):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda _pid, _birth: False,
            shutdown_sender=lambda *_args: {"version": 1, "ok": True, "operation": "shutdown"},
        )

    if failure_stage == "rollback_failure":
        assert installed_path.read_text(encoding="utf-8") == unit.text
    else:
        assert installed_path.read_bytes() == before_bytes
        assert stat.S_IMODE(installed_path.stat().st_mode) == before_mode
    assert [call[2] for call in calls] == ["is-active", "show"]
    check = sqlite3.connect(ledger_path)
    try:
        expected_version = "21" if failure_stage == "rollback_failure" else "18"
        assert (
            check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
            == expected_version
        )
        if failure_stage == "rollback_failure":
            assert check.execute("SELECT 1 FROM sqlite_master WHERE name = 'supervisor_authority'").fetchone() is None
            assert (
                check.execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()[0] == 1
            )
        else:
            assert (
                check.execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()[0]
                == "codex_flow_event_driven_program_controller_v18"
            )
            assert (
                check.execute("SELECT requested_shutdown FROM supervisor_authority WHERE singleton = 1").fetchone()[0]
                == 1
            )
            assert check.execute("SELECT 1 FROM sqlite_master WHERE name = 'harness_authority'").fetchone() is None
    finally:
        check.close()

    monkeypatch.undo()
    retry_calls: list[tuple[str, ...]] = []
    replacement = {"started": False}

    def retry_runner(argv: tuple[str, ...], **_: object) -> Result:
        retry_calls.append(argv)
        operation = argv[2]
        if operation == "show":
            result = Result(0)
            result.stdout = (
                "ActiveState=active\nMainPID=1\n" if replacement["started"] else "ActiveState=inactive\nMainPID=0\n"
            )
            return result
        if operation == "is-active":
            return Result(0 if replacement["started"] else 3)
        if operation == "start":
            replacement["started"] = True
            migrated = Ledger(ledger_path)
            try:
                migrated.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=_TEST_REPLACEMENT_PID,
                    process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                    executable_digest=_TEST_INTERPRETER_DIGEST,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                migrated.close()
        return Result(0)

    result = refresh_with_credential(
        unit,
        config_home=config_home,
        environment={"OPENAI_API_KEY": "secret"},
        runner=retry_runner,
        process_is_live=lambda pid, birth: (
            pid == _TEST_REPLACEMENT_PID and birth == _TEST_REPLACEMENT_BIRTH and replacement["started"]
        ),
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
        shutdown_sender=lambda *_args: pytest.fail("stopped predecessor must not be shut down again"),
    )
    assert result["refreshed"] is True
    assert replacement["started"] is True
    assert [call[2] for call in retry_calls] == [
        "is-active",
        "show",
        *(["show"] if failure_stage == "rollback_failure" else []),
        "daemon-reload",
        "import-environment",
        "show",
        "start",
        "is-active",
    ]
    migrated = Ledger(ledger_path)
    try:
        assert migrated.schema_version.value == 21
        assert migrated.harness_authority() is not None
    finally:
        migrated.close()


def test_v18_refresh_rejects_dangling_or_new_socket_symlink_before_start(tmp_path: Path) -> None:
    repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    # Convert the fixture's v19 ledger/unit pair into a disposable v18
    # predecessor, then leave a legacy socket directory entry behind.
    ledger.close()
    ledger_path = repository / ".codex-flow" / "workflow.db"
    connection = sqlite3.connect(ledger_path)
    connection.execute("DROP TABLE dispatch_terminal_integrity")
    connection.execute("ALTER TABLE harness_authority RENAME TO supervisor_authority")
    connection.execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
    connection.execute(
        "UPDATE schema_meta SET value = 'codex_flow_event_driven_program_controller_v18' WHERE key = 'schema_identity'"
    )
    connection.commit()
    connection.close()
    legacy = service_module._legacy_supervisor_unit(unit)
    config_home = tmp_path / "config"
    install_unit(legacy, config_home=config_home)
    runtime = repository / ".codex-flow" / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "supervisor.sock").symlink_to(runtime / "harness.sock")
    calls: list[tuple[str, ...]] = []

    class Result:
        returncode = 3
        stdout = ""

    with pytest.raises(ServiceRefreshFailed, match="must not be a symlink"):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=lambda argv, **_: calls.append(argv) or Result(),
            process_is_live=lambda _pid, _birth: False,
            ledger=Ledger(ledger_path, allow_legacy=True),
        )
    assert [call[2] for call in calls] == []
    assert processes[(500, "old-birth")] is True
    assert service_state["active"] is True


def test_refresh_manager_live_pid_unproven(tmp_path: Path) -> None:
    _repository, unit, ledger, processes, service_state = _refresh_fixture(tmp_path)
    calls: list[str] = []

    class Result:
        returncode = 0
        stdout = f"ActiveState=inactive\nMainPID={os.getpid()}\n"

    def runner(argv: tuple[str, ...], **_: object) -> object:
        calls.append(argv[2])
        if argv[2] == "is-active":
            return _SystemctlResult(3)
        return Result()

    processes[(500, "old-birth")] = False
    service_state["active"] = False
    try:
        with pytest.raises(ServiceRefreshDeferred, match="stopped"):
            refresh_with_credential(
                unit,
                ledger=ledger,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda *_: False,
                clock=lambda: 0.0,
                sleeper=lambda _: pytest.fail("must reject before waiting or starting"),
            )
        assert "start" not in calls
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "response, returncode",
    [
        ("", 0),
        ("ActiveState=inactive\n", 0),
        ("MainPID=0\n", 0),
        ("ActiveState=inactive\nActiveState=inactive\n", 0),
        ("MainPID=0\nMainPID=0\n", 0),
        ("ActiveState=inactive\nOther=0\n", 0),
        ("ActiveState=inactive\nMainPID=0\nExtra=0", 0),
        ("ActiveState=inactive\nMainPID=0\n\n", 0),
        ("ActiveState=inactive\r\nMainPID=0\n", 0),
        (" ActiveState=inactive\nMainPID=0", 0),
        ("ActiveState=inactive \nMainPID=0", 0),
        ("ActiveState=inactive\nMainPID=+0", 0),
        ("ActiveState=inactive\nMainPID=-1", 0),
        ("ActiveState=inactive\nMainPID=00", 0),
        ("ActiveState=inactive\nMainPID=0.0", 0),
        ("ActiveState=inactive\nMainPID=\u0660", 0),
        ("ActiveState=inactive\nMainPID=10000000000", 0),
        ("ActiveState=inactive\nMainPID=0=0", 0),
        ("ActiveState=" + "a" * 256 + "\nMainPID=0", 0),
        ("ActiveState=failed\nMainPID=0", 0),
        ("ActiveState=deactivating\nMainPID=0", 0),
        ("ActiveState=activating\nMainPID=0", 0),
        ("ActiveState=reloading\nMainPID=0", 0),
        ("ActiveState=unknown\nMainPID=0", 0),
        (f"ActiveState=inactive\nMainPID={os.getpid()}", 0),
        ("ActiveState=inactive\nMainPID=0", 3),
        ("ActiveState=inactive\nMainPID=0", True),
        ("ActiveState=inactive\nMainPID=0", "0"),
        (b"ActiveState=inactive\nMainPID=0", 0),
        (None, 0),
    ],
)
@pytest.mark.parametrize("topology", ["current", "predecessor", "interrupted"])
def test_refresh_rejects_unproven_manager_before_any_effect(
    tmp_path: Path,
    response: object,
    returncode: object,
    topology: str,
) -> None:
    from types import SimpleNamespace

    if topology == "interrupted":
        _repository, unit, ledger_path, config_home, installed = _interrupted_refresh_fixture(tmp_path)
    else:
        _repository, unit, ledger_path, config_home, _executable, _processes, _state = _same_topology_refresh_fixture(
            tmp_path,
            21 if topology == "current" else 19,
            fenced=True,
        )
        installed = unit_path(repository_root=unit.repository_root, config_home=config_home)
    before = installed.read_bytes()
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **kwargs: object) -> object:
        calls.append(argv)
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        if argv[2] == "show":
            assert argv == (
                "systemctl",
                "--user",
                "show",
                unit.unit_name,
                "--property=ActiveState",
                "--property=MainPID",
                "--no-pager",
            )
            return SimpleNamespace(returncode=returncode, stdout=response)
        assert argv[2] == "is-active"
        return _SystemctlResult(3)

    with pytest.raises(ServiceError):
        refresh_with_credential(
            unit,
            config_home=config_home,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda *_: False,
            sleeper=lambda _: pytest.fail("malformed/final proof must not wait"),
        )
    assert [call[2] for call in calls].count("show") == 1
    assert installed.read_bytes() == before
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == (
            "19" if topology == "predecessor" else "21"
        )
        assert connection.execute("SELECT requested_shutdown FROM harness_authority").fetchone()[0] == 1


@pytest.mark.parametrize("proof_number", [1, 2])
def test_refresh_final_manager_observation_blocks_changed_main_pid(tmp_path: Path, proof_number: int) -> None:
    _repository, unit, ledger, _processes, _state = _refresh_fixture(tmp_path)
    shows = 0
    effects: list[str] = []

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        nonlocal shows
        operation = argv[2]
        effects.append(operation)
        if operation == "show":
            shows += 1
            pid = os.getpid() if shows == proof_number else 0
            return _SystemctlResult(0, f"ActiveState=inactive\nMainPID={pid}\n")
        return _SystemctlResult(3 if operation == "is-active" else 0)

    try:
        with pytest.raises(ServiceRefreshDeferred, match="stopped"):
            refresh_with_credential(
                unit,
                ledger=ledger,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda *_: False,
            )
        assert shows == proof_number
        assert "start" not in effects
        assert ledger.harness_refresh_fenced()
    finally:
        ledger.close()


@pytest.mark.parametrize("missing", ["stdout", "returncode"])
def test_refresh_manager_structural_result_requires_both_properties(tmp_path: Path, missing: str) -> None:
    from types import SimpleNamespace

    _repository, unit, ledger, _processes, _state = _refresh_fixture(tmp_path)
    result = (
        SimpleNamespace(stdout="ActiveState=inactive\nMainPID=0")
        if missing == "returncode"
        else SimpleNamespace(returncode=0)
    )
    try:
        with pytest.raises(ServiceRefreshFailed):
            refresh_with_credential(
                unit,
                ledger=ledger,
                config_home=tmp_path / "config",
                environment={"OPENAI_API_KEY": "secret"},
                runner=lambda *_args, **_kwargs: result,
                process_is_live=lambda *_: False,
            )
    finally:
        ledger.close()


@pytest.mark.parametrize("topology", ["current", "interrupted"])
def test_refresh_rollback_cannot_restore_with_unproven_manager_pid(tmp_path: Path, topology: str) -> None:
    if topology == "current":
        _repository, unit, ledger, _processes, _state = _refresh_fixture(tmp_path)
        config_home = tmp_path / "config"
    else:
        _repository, unit, ledger_path, config_home, _installed = _interrupted_refresh_fixture(tmp_path)
        ledger = Ledger(ledger_path)
    started = False
    stopped = False

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        nonlocal started, stopped
        operation = argv[2]
        if operation == "show":
            return _SystemctlResult(0, f"ActiveState=inactive\nMainPID={os.getpid() if stopped else 0}\n")
        if operation == "is-active":
            return _SystemctlResult(0 if started and not stopped else 3)
        if operation == "start":
            started = True
            ledger.acquire_harness(
                repository_root=unit.repository_root,
                state_root=unit.state_root,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest="f" * 64,
                version=unit.version,
                owner_nonce_sha256="d" * 64,
            )
        if operation == "stop":
            stopped = True
        return _SystemctlResult(0)

    try:
        with pytest.raises(ServiceError):
            refresh_with_credential(
                unit,
                ledger=ledger,
                config_home=config_home,
                environment={"OPENAI_API_KEY": "secret"},
                runner=runner,
                process_is_live=lambda pid, _birth: started and not stopped and pid == _TEST_REPLACEMENT_PID,
                sleeper=lambda _: pytest.fail("rollback must reject its final manager proof"),
            )
        assert started and stopped
        assert ledger.harness_refresh_fenced()
        # The failed replacement remains fenced under the current unit; no legacy restoration is safe.
        assert unit_path(repository_root=unit.repository_root, config_home=config_home).read_text() == unit.text
    finally:
        ledger.close()


@pytest.mark.parametrize("schema_version", [20, 21])
@pytest.mark.parametrize(
    "failure_stage",
    [
        "active-generation",
        "authorize-before",
        "authorize-after",
        "publish-before",
        "publish-after",
        "daemon-reload",
        "import-environment",
        "start",
    ],
)
def test_profile_rotation_failure_reentry_preserves_unit_and_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int, failure_stage: str
) -> None:
    import json

    from codex_flow.domain import ControllerGenerationState

    repository, _unit, ledger_path, config_home, executable, processes, service_state = _same_topology_refresh_fixture(
        tmp_path, 21
    )
    processes[(500, "old-birth")] = False
    service_state["active"] = False
    old_unit = generate_unit(
        repository, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="c" * 64
    )
    target = generate_unit(
        repository, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="e" * 64
    )
    path = install_unit(old_unit, config_home=config_home)
    original_authorize = Ledger.authorize_controller_profile_refresh
    original_install = service_module.install_unit
    armed = True
    events: list[str] = []
    # Real decisions exercise the existing authorization boundary, including an
    # identified terminal generation which must retain all its historical facts.
    ledger = Ledger(ledger_path, allow_legacy=True)
    decisions = []
    for name in ("eligible", "identified"):
        ledger.create_run(name)
        ledger.create_milestone(name, "repair")
        dispatch = ledger.claim_dispatch(name, "repair", "executor", 1).dispatch_id
        ledger.enqueue_dispatch(
            dispatch,
            backend="sdk_headless",
            capsule_json='{"model":"test","prompt":"bounded"}',
            route_json=json.dumps({"native_profile_sha256": "c" * 64, "native_compatibility_sha256": "b" * 64}),
            workspace_path=repository,
            result_contract_sha256="a" * 64,
            source_thread_id="source",
            native_profile_sha256="c" * 64,
            native_compatibility_sha256="b" * 64,
        )
        decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source")
        wake = ledger.claim_wake(f"wake/{dispatch}/checkpoint")
        assert wake is not None
        ledger.record_wake_delivery(str(wake["delivery_id"]), outcome="delivered", source_turn_id="wake-turn")
        if name == "eligible":
            ledger.prepare_controller_generation(decision.decision_id, generation=1)
            ledger.record_controller_profile_drift(decision.decision_id, generation=1)
        if name == "identified":
            ledger.prepare_controller_generation(decision.decision_id, generation=1)
            ledger.bind_controller_generation_thread(
                decision.decision_id, generation=1, controller_thread_id="identified-thread"
            )
            if failure_stage != "active-generation":
                ledger.complete_controller_generation(
                    decision.decision_id, generation=1, state=ControllerGenerationState.FAILED
                )
        decisions.append(decision)
    identified = decisions[1]

    def identified_snapshot(current: Ledger) -> tuple[object, ...]:
        return (
            current.controller_decision(identified.decision_id),
            current.controller_generation(identified.decision_id, 1),
            current.queue_binding(identified.dispatch_id),
            current.queue_dispatch(identified.dispatch_id),
        )

    historical = identified_snapshot(ledger)
    if failure_stage != "active-generation":
        ledger.arm_harness_refresh_fence()
    ledger.close()
    if schema_version == 20:
        with sqlite3.connect(ledger_path) as connection:
            connection.execute("DROP TABLE program_outcomes")
            connection.execute("UPDATE schema_meta SET value = '20' WHERE key = 'schema_version'")
            connection.execute(
                "UPDATE schema_meta SET value = 'codex_flow_dispatch_terminal_integrity_v20' WHERE key = 'schema_identity'"
            )

    def authorize(self: Ledger, **kwargs: str) -> int:
        events.append("authorize")
        assert path.read_bytes() in (old_unit.text.encode(), target.text.encode())
        if armed and failure_stage == "authorize-before":
            raise RuntimeError("injected authorization failure")
        result = original_authorize(self, **kwargs)
        assert identified_snapshot(self) == historical
        if armed and failure_stage == "authorize-after":
            raise RuntimeError("injected committed authorization failure")
        return result

    def publish(unit, **kwargs):
        events.append("publish")
        assert "authorize" in events
        if armed and failure_stage == "publish-before":
            raise RuntimeError("injected publication failure")
        result = original_install(unit, **kwargs)
        if armed and failure_stage == "publish-after":
            raise RuntimeError("injected published unit failure")
        return result

    monkeypatch.setattr(Ledger, "authorize_controller_profile_refresh", authorize)
    monkeypatch.setattr(service_module, "install_unit", publish)

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        events.append(operation)
        result = _SystemctlResult(0)
        if operation == "show":
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
        if operation == "is-active":
            return _SystemctlResult(0 if service_state["active"] else 3)
        if armed and operation == failure_stage:
            return _SystemctlResult(1)
        if operation == "start":
            assert path.read_bytes() == target.text.encode()
            service_state["active"] = True
            processes[(_TEST_REPLACEMENT_PID, _TEST_REPLACEMENT_BIRTH)] = True
            replacement = Ledger(ledger_path)
            replacement.acquire_harness(
                repository_root=repository,
                state_root=repository,
                pid=_TEST_REPLACEMENT_PID,
                process_birth_identity=_TEST_REPLACEMENT_BIRTH,
                executable_digest=_TEST_INTERPRETER_DIGEST,
                version=target.version,
                owner_nonce_sha256="d" * 64,
            )
            replacement.close()
        return result

    def refresh() -> dict[str, object]:
        return refresh_with_credential(
            target,
            config_home=config_home,
            profile_sha256=target.profile_sha256,
            native_compatibility_sha256="d" * 64,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda pid, birth: processes.get((pid, birth), False),
        )

    if failure_stage == "active-generation":
        with pytest.raises(ServiceRefreshDeferred):
            refresh()
        assert path.read_bytes() == old_unit.text.encode()
        assert "authorize" not in events and "publish" not in events and "start" not in events
        check = Ledger(ledger_path, allow_legacy=True)
        assert identified_snapshot(check) == historical
        assert check.queue_binding(decisions[0].dispatch_id)["native_profile_sha256"] == "c" * 64
        check.close()
        return

    with pytest.raises(ServiceRefreshFailed):
        refresh()
    published = failure_stage in {"publish-after", "daemon-reload", "import-environment", "start"}
    assert path.read_bytes() == (target if published else old_unit).text.encode()
    assert events.count("start") == int(failure_stage == "start")
    check = Ledger(ledger_path)
    assert check.schema_version.value == 21
    assert check.harness_refresh_fenced()
    assert identified_snapshot(check) == historical
    binding = check.queue_binding(decisions[0].dispatch_id)
    assert binding["native_profile_sha256"] == ("c" if failure_stage == "authorize-before" else "e") * 64
    check.close()
    armed = False
    events.clear()
    assert refresh()["refreshed"] is True
    assert events.count("start") == 1
    check = Ledger(ledger_path)
    assert identified_snapshot(check) == historical
    assert check.queue_binding(decisions[0].dispatch_id)["native_profile_sha256"] == "e" * 64
    check.close()


@pytest.mark.parametrize("mutation", ["profile", "mode", "target", "target-mode", "missing-compatibility"])
def test_profile_rotation_rejects_unit_drift_before_publication_or_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    repository, _unit, ledger_path, config_home, executable, _processes, _state = _same_topology_refresh_fixture(
        tmp_path, 20, fenced=True
    )
    old = generate_unit(repository, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="c" * 64)
    target = generate_unit(
        repository, executable=executable, provider_env_key="OPENAI_API_KEY", profile_sha256="e" * 64
    )
    path = install_unit(old, config_home=config_home)
    original_install = service_module.install_unit
    calls: list[str] = []

    def publish(unit, **kwargs):
        calls.append("publish")
        result = original_install(unit, **kwargs)
        if mutation == "target":
            path.write_text(old.text)
        return result

    monkeypatch.setattr(service_module, "install_unit", publish)

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        calls.append(operation)
        if operation == "show":
            if mutation == "profile":
                path.write_text(old.text.replace("c" * 64, "f" * 64))
            elif mutation == "mode":
                path.chmod(0o400)
            result = _SystemctlResult(0)
            result.stdout = "ActiveState=inactive\nMainPID=0\n"
            return result
        if operation == "daemon-reload" and mutation == "target-mode":
            path.chmod(0o400)
        return _SystemctlResult(3 if operation == "is-active" else 0)

    with pytest.raises(ServiceError):
        refresh_with_credential(
            target,
            config_home=config_home,
            profile_sha256=target.profile_sha256,
            native_compatibility_sha256=None if mutation == "missing-compatibility" else "d" * 64,
            environment={"OPENAI_API_KEY": "secret"},
            runner=runner,
            process_is_live=lambda *_: False,
        )
    assert "start" not in calls
    assert ("publish" in calls) == mutation.startswith("target")
    check = Ledger(ledger_path, allow_legacy=True)
    assert check.schema_version.value == (21 if mutation.startswith("target") else 20)
    check.close()
