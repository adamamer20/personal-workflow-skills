from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
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


def _interrupted_refresh_fixture(tmp_path: Path) -> tuple[Path, object, Path, Path, Path]:
    """Build a fenced v19 ledger paired with its exact stopped v18 unit."""

    repository = tmp_path / "repo"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
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
    assert [call[2] for call in calls][:1] == ["is-active"]
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
        if operation == "is-active":
            return _SystemctlResult(0 if replacement_started else 3)
        if operation == "start":
            replacement_started = True
            replacement = Ledger(ledger_path)
            try:
                replacement.acquire_harness(
                    repository_root=unit.repository_root,
                    state_root=unit.state_root,
                    pid=501,
                    process_birth_identity="new-birth",
                    executable_digest="c" * 64,
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
        process_is_live=lambda pid, birth: pid == 501 and birth == "new-birth" and replacement_started,
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
            runner=lambda argv, **_: calls.append(argv) or _SystemctlResult(3),
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
                "process_birth_identity": "replacement-birth",
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        nonlocal started, service_active
        calls.append(argv)
        operation = argv[2]
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
                process_is_live=lambda pid, birth: started and pid == 501 and birth == "replacement-birth",
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
    process_live = {(500, "old-birth"): False, (501, "replacement-birth"): False}
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
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
                    pid=501,
                    process_birth_identity="replacement-birth",
                    executable_digest="c" * 64,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                acquired.close()
        elif operation == "stop":
            service_active["value"] = False
            process_live[(501, "replacement-birth")] = False
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
        "is-active",
        "is-active",
        "daemon-reload",
        "import-environment",
        "start",
        "is-active",
        "stop",
        "is-active",
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
        assert authority["pid"] == 501
        assert authority["process_birth_identity"] == "replacement-birth"
        assert authority["requested_shutdown"] == 1
    finally:
        check.close()

    retry_started = {"value": False}

    def retry_runner(argv: tuple[str, ...], **_: object) -> Result:
        operation = argv[2]
        if operation == "is-active":
            return Result(0 if retry_started["value"] else 3)
        if operation == "start":
            retry_started["value"] = True
            acquired = Ledger(ledger_path)
            try:
                acquired.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=502,
                    process_birth_identity="retry-birth",
                    executable_digest="e" * 64,
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
        process_is_live=lambda pid, birth: pid == 502 and birth == "retry-birth" and retry_started["value"],
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
    process_live = {(501, "replacement-birth"): requested_shutdown == 1}
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        operation = argv[2]
        if operation == "is-active":
            return Result(0 if service_active["value"] else 3)
        if operation == "start":
            replacement = Ledger(ledger_path)
            try:
                replacement.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=501,
                    process_birth_identity="replacement-birth",
                    executable_digest="c" * 64,
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
            process_live[(501, "replacement-birth")] = False
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
            "is-active",
            "is-active",
            "daemon-reload",
            "import-environment",
            "start",
            "is-active",
            "stop",
            "is-active",
            "unset-environment",
        ]
        if requested_shutdown == 0
        else [
            "is-active",
            "is-active",
            "daemon-reload",
            "import-environment",
            "start",
            "stop",
            "is-active",
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        operation = argv[2]
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
        "pid": 501,
        "process_birth_identity": "replacement-birth",
        "epoch": int(predecessor["epoch"]) + 1,
        "requested_shutdown": 0,
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
            process_is_live=lambda pid, birth: (pid, birth) == (501, "replacement-birth"),
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


def test_refresh_rejects_replacement_to_different_replacement_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a replacement is acquired, a different replacement cannot win rollback."""

    repository, unit, ledger_path, config_home, installed_path = _interrupted_refresh_fixture(tmp_path)
    ledger = Ledger(ledger_path)
    predecessor = ledger.harness_authority()
    assert predecessor is not None
    replacement_a = {
        **predecessor,
        "pid": 501,
        "process_birth_identity": "replacement-a",
        "epoch": int(predecessor["epoch"]) + 1,
        "requested_shutdown": 0,
    }
    replacement_b = {
        **predecessor,
        "pid": 502,
        "process_birth_identity": "replacement-b",
        "epoch": int(predecessor["epoch"]) + 2,
        "requested_shutdown": 1,
    }
    # The interrupted-recovery preflight must still see the fenced
    # predecessor before the acquired replacement and its drifted successor.
    observations = iter((predecessor, replacement_a, replacement_b))

    def observed_authority() -> object:
        return next(observations, replacement_b)

    monkeypatch.setattr(ledger, "harness_authority", observed_authority)
    service_active = {"value": False}

    def runner(argv: tuple[str, ...], **_: object) -> _SystemctlResult:
        operation = argv[2]
        if operation == "is-active":
            return _SystemctlResult(0 if service_active["value"] else 3)
        if operation == "start":
            service_active["value"] = True
            acquired = Ledger(ledger_path)
            try:
                acquired.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=501,
                    process_birth_identity="replacement-a",
                    executable_digest="c" * 64,
                    version=unit.version,
                    owner_nonce_sha256="d" * 64,
                )
            finally:
                acquired.close()
        elif operation == "stop":
            service_active["value"] = False
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
        if argv[2] == "is-active":
            return type("InactiveResult", (), {"returncode": 3})()
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
    assert [call[2] for call in calls] == ["is-active"]


@pytest.mark.parametrize("failure_stage", ["before_commit", "after_commit", "rollback_failure"])
def test_v18_refresh_migration_opener_failure_restores_legacy_pair_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    executable = tmp_path / "codex-flow"
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

    def runner(argv: tuple[str, ...], **_: object) -> Result:
        calls.append(argv)
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
    assert [call[2] for call in calls] == ["is-active"]
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
        if operation == "is-active":
            return Result(0 if replacement["started"] else 3)
        if operation == "start":
            replacement["started"] = True
            migrated = Ledger(ledger_path)
            try:
                migrated.acquire_harness(
                    repository_root=repository,
                    state_root=repository,
                    pid=501,
                    process_birth_identity="new-birth",
                    executable_digest="c" * 64,
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
        process_is_live=lambda pid, birth: pid == 501 and birth == "new-birth" and replacement["started"],
        clock=lambda: 0.0,
        sleeper=lambda _delay: None,
        shutdown_sender=lambda *_args: pytest.fail("stopped predecessor must not be shut down again"),
    )
    assert result["refreshed"] is True
    assert replacement["started"] is True
    assert [call[2] for call in retry_calls] == [
        "is-active",
        "daemon-reload",
        "import-environment",
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
