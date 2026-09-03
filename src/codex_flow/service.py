"""Per-repository user-service templates and explicit lifecycle helpers."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from .ipc import IpcError, send_request
from .ledger import CURRENT_SCHEMA_VERSION, HarnessRefreshBlocked, Ledger, LedgerError, ledger_schema_compatibility


class ServiceError(RuntimeError):
    """A service template or lifecycle operation is unsafe."""


class CredentialUnavailable(ServiceError):
    """The profile-selected credential is absent from the invoking process."""


class ServiceStartFailed(ServiceError):
    """The user service could not be started after credential import."""


class ServiceRefreshDeferred(ServiceError):
    """A refresh was refused because a worker or controller child is active."""


class ServiceRefreshFailed(ServiceError):
    """A bounded harness handoff did not reach a verified replacement."""


@runtime_checkable
class _CommandResult(Protocol):
    returncode: int


RefreshShutdown = Callable[[Path, float], dict[str, object]]
ProcessLiveness = Callable[[int, str], bool]


def process_birth_identity(pid: int) -> str:
    """Read Linux's process birth discriminator without trusting a PID alone."""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return fields[21]
    except (OSError, IndexError, ValueError):
        return f"pid:{pid}"


def _exact_process_is_live(pid: int, birth_identity: str, identity_reader: Callable[[int], str]) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return identity_reader(pid) == birth_identity


def _manager_returncode(result: object, *, operation: str) -> int:
    if (
        not isinstance(result, _CommandResult)
        or isinstance(result.returncode, bool)
        or not isinstance(result.returncode, int)
    ):
        raise ServiceRefreshFailed(f"user service {operation} returned no valid exit status")
    return result.returncode


def _run_manager(
    arguments: tuple[str, ...],
    *,
    runner: Callable[..., object],
) -> int:
    try:
        result = runner(("systemctl", "--user", *arguments), check=False, capture_output=True, text=True)
    except OSError as exc:
        operation = arguments[0] if arguments else "operation"
        raise ServiceRefreshFailed(f"user service {operation} command failed") from exc
    operation = arguments[0] if arguments else "operation"
    return _manager_returncode(result, operation=operation)


def _unit_is_active(unit: ServiceUnit, *, runner: Callable[..., object]) -> bool:
    status = _run_manager(("is-active", "--quiet", unit.unit_name), runner=runner)
    if status == 0:
        return True
    # systemctl's documented inactive result is the only non-zero status that
    # proves the unit is gone/inactive.  Treat manager errors and unknown-unit
    # responses as lifecycle failures instead of allowing refresh to proceed on
    # an unproven service boundary.
    if status == 3:
        return False
    raise ServiceRefreshFailed("user service activity check failed")


def _send_authenticated_shutdown(socket_path: Path, timeout: float) -> dict[str, object]:
    try:
        response = send_request(
            socket_path,
            {"version": 1, "operation": "shutdown"},
            timeout=timeout,
        )
    except (IpcError, OSError) as exc:
        raise ServiceRefreshFailed("authenticated harness shutdown failed") from exc
    if response != {"version": 1, "ok": True, "operation": "shutdown"}:
        raise ServiceRefreshFailed("authenticated harness shutdown returned an invalid acknowledgement")
    return response


def _authority_identity(authority: Mapping[str, object]) -> tuple[int, str, int]:
    try:
        pid = authority["pid"]
        birth_identity = authority["process_birth_identity"]
        epoch = authority["epoch"]
    except KeyError as exc:
        raise ServiceRefreshFailed("harness authority is incomplete") from exc
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(birth_identity, str)
        or not birth_identity
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch <= 0
    ):
        raise ServiceRefreshFailed("harness authority has invalid process identity")
    return pid, birth_identity, epoch


def _authority_matches_unit(authority: Mapping[str, object], unit: ServiceUnit) -> None:
    for field, expected in (
        ("repository_root", os.fspath(unit.repository_root.resolve())),
        ("state_root", os.fspath(unit.state_root.resolve())),
        ("version", unit.version),
    ):
        if authority.get(field) != expected:
            raise ServiceRefreshFailed(f"harness authority {field} does not match the repository unit")


def _replacement_is_healthy(
    authority: Mapping[str, object] | None,
    *,
    unit: ServiceUnit,
    old_pid: int,
    old_birth_identity: str,
    old_epoch: int,
    process_is_live: ProcessLiveness,
) -> bool:
    if authority is None:
        return False
    _authority_matches_unit(authority, unit)
    try:
        pid = authority["pid"]
        birth_identity = authority["process_birth_identity"]
        epoch = authority["epoch"]
        requested_shutdown = authority["requested_shutdown"]
    except KeyError as exc:
        raise ServiceRefreshFailed("replacement harness authority is incomplete") from exc
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(birth_identity, str)
        or not birth_identity
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch <= old_epoch
        or requested_shutdown != 0
    ):
        return False
    if pid == old_pid and birth_identity == old_birth_identity:
        return False
    return process_is_live(pid, birth_identity)


def _unit_arg(path: Path) -> str:
    """Quote one absolute path for systemd's ExecStart grammar."""

    raw = os.fspath(path)
    if any(character in raw for character in "\x00\r\n"):
        raise ServiceError("service path contains a forbidden control character")
    value = raw.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"' if any(character.isspace() for character in value) else value


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ServiceError("service path contains a symlink")


@dataclass(frozen=True, slots=True)
class ServiceUnit:
    repository_root: Path
    state_root: Path
    executable: Path
    version: str
    unit_name: str
    text: str
    provider_env_key: str | None = None
    profile_sha256: str | None = None
    runtime: str = "harness"


def unit_name(repository_root: Path) -> str:
    digest = hashlib.sha256(os.fspath(repository_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"codex-flow-{digest}.service"


def generate_unit(
    repository_root: Path,
    *,
    state_root: Path | None = None,
    executable: Path | None = None,
    version: str = "0.2.0",
    provider_env_key: str | None = None,
    profile_sha256: str | None = None,
) -> ServiceUnit:
    repository_root = repository_root.resolve(strict=True)
    state_root = (state_root or repository_root).resolve(strict=True)
    if any(character in os.fspath(repository_root) for character in "\x00\r\n") or any(
        character in os.fspath(state_root) for character in "\x00\r\n"
    ):
        raise ServiceError("service path contains a forbidden control character")
    if executable is None:
        raise ServiceError("service executable must be an exact absolute installed path")
    executable = Path(executable)
    if not executable.is_absolute():
        raise ServiceError("service executable does not exist")
    try:
        executable_metadata = executable.lstat()
    except OSError as exc:
        raise ServiceError("service executable does not exist") from exc
    if (
        stat.S_ISLNK(executable_metadata.st_mode)
        or not stat.S_ISREG(executable_metadata.st_mode)
        or executable_metadata.st_nlink != 1
        or not (executable_metadata.st_mode & stat.S_IXUSR)
    ):
        raise ServiceError("service executable must be a single-link executable file")
    if not version or any(c in version for c in "\r\n"):
        raise ServiceError("service version is invalid")
    if provider_env_key is not None and re.fullmatch(r"[A-Z][A-Z0-9_]*", provider_env_key) is None:
        raise ServiceError("provider environment key name is invalid")
    if profile_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", profile_sha256) is None:
        raise ServiceError("provider profile identity is invalid")
    name = unit_name(repository_root)
    pass_environment = f"PassEnvironment={provider_env_key}\n" if provider_env_key is not None else ""
    profile_identity = f"# Codex-Flow-Profile-SHA256={profile_sha256}\n" if profile_sha256 is not None else ""
    text = "".join(
        (
            "[Unit]\n",
            f"Description=Codex Flow harness ({repository_root})\n",
            "After=default.target\n\n",
            "[Service]\n",
            profile_identity,
            "Type=simple\n",
            f"ExecStart={_unit_arg(executable)} harness run --foreground --state-root {_unit_arg(state_root)}\n",
            pass_environment,
            "Restart=on-failure\n",
            "RuntimeDirectory=codex-flow\n",
            "NoNewPrivileges=yes\n\n",
            "[Install]\nWantedBy=default.target\n",
        )
    )
    return ServiceUnit(repository_root, state_root, executable, version, name, text, provider_env_key, profile_sha256)


def _legacy_supervisor_unit(unit: ServiceUnit) -> ServiceUnit:
    """Describe the exact v18 predecessor without making it a runtime alias."""

    if unit.runtime != "harness":
        raise ServiceError("replacement service unit runtime is not harness")
    old_text = unit.text.replace("Description=Codex Flow harness ", "Description=Codex Flow supervisor ", 1)
    old_text = old_text.replace(" harness run --foreground", " supervisor run --foreground", 1)
    if old_text == unit.text or "supervisor run --foreground" not in old_text:
        raise ServiceError("legacy supervisor service template cannot be derived")
    return ServiceUnit(
        unit.repository_root,
        unit.state_root,
        unit.executable,
        unit.version,
        unit.unit_name,
        old_text,
        unit.provider_env_key,
        unit.profile_sha256,
        "supervisor",
    )


def unit_path(*, config_home: Path | None = None, repository_root: Path) -> Path:
    base = config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / unit_name(repository_root)


def install_unit(unit: ServiceUnit, *, config_home: Path | None = None) -> Path:
    path = unit_path(config_home=config_home, repository_root=unit.repository_root)
    _assert_no_symlink_ancestors(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ServiceError("service unit path is not a private regular file")
    path.write_text(unit.text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def systemctl_user(action: str, unit: ServiceUnit, *, runner=subprocess.run) -> None:
    if action not in {"start", "stop", "status", "enable", "disable"}:
        raise ServiceError("unsupported user-service action")
    result = runner(("systemctl", "--user", action, unit.unit_name), check=False, capture_output=True, text=True)
    if result.returncode != 0 and action != "status":
        raise ServiceError("user service action failed")


def _require_profile_credential(
    unit: ServiceUnit,
    *,
    provider_env_key: str | None,
    profile_sha256: str | None,
    environment: Mapping[str, str] | None,
) -> tuple[str, str]:
    key = unit.provider_env_key if provider_env_key is None else provider_env_key
    if key is None or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
        raise ServiceError("profile-selected provider environment key is missing")
    if unit.provider_env_key != key:
        raise ServiceError("provider environment key conflicts with the exact service unit")
    source = os.environ if environment is None else environment
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise CredentialUnavailable("profile-selected provider credential is unavailable")
    return key, value


def _assert_existing_unit(unit: ServiceUnit, *, config_home: Path | None = None) -> Path:
    """Require the exact repository unit before a refresh may mutate it."""

    path = unit_path(config_home=config_home, repository_root=unit.repository_root)
    _assert_no_symlink_ancestors(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ServiceError("matching repository harness unit is not installed") from exc
    except OSError as exc:
        raise ServiceError("matching repository harness unit is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ServiceError("matching repository harness unit is unsafe")
    return path


def _read_installed_unit(path: Path) -> tuple[bytes, str]:
    """Read one private installed unit without following a replacement link."""

    _assert_no_symlink_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ServiceError("installed service unit is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ServiceError("installed service unit is not a private regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(131_073)
    except ServiceError:
        raise
    except OSError as exc:
        raise ServiceError("installed service unit cannot be read") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > 131_072:
        raise ServiceError("installed service unit is oversized")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServiceError("installed service unit is not valid UTF-8") from exc
    return raw, text


def _validate_installed_unit(
    unit: ServiceUnit,
    *,
    config_home: Path | None = None,
    expected_profile_sha256: str | None = None,
    credential_value: str | None = None,
    allow_profile_identity_update: bool = False,
) -> str:
    """Validate the on-disk unit identity before any manager mutation."""

    if unit.unit_name != unit_name(unit.repository_root):
        raise ServiceError("service unit identity is inconsistent")
    if expected_profile_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_profile_sha256) is None:
        raise ServiceError("provider profile identity is invalid")
    if expected_profile_sha256 != unit.profile_sha256:
        raise ServiceError("provider profile identity conflicts with the exact service unit")
    path = unit_path(config_home=config_home, repository_root=unit.repository_root)
    raw, text = _read_installed_unit(path)
    expected = unit.text.encode("utf-8")
    if credential_value and (credential_value in text or credential_value in unit.text):
        raise ServiceError("service unit contains credential material")
    lines = text.splitlines()
    expected_lines = unit.text.splitlines()
    if unit.runtime not in {"harness", "supervisor"}:
        raise ServiceError("service unit runtime is unsupported")
    expected_exec = f"ExecStart={_unit_arg(unit.executable)} {unit.runtime} run --foreground --state-root {_unit_arg(unit.state_root)}"
    exec_lines = [line for line in lines if line.startswith("ExecStart=")]
    if exec_lines != [expected_exec]:
        raise ServiceError("installed service unit ExecStart drifted")
    pass_lines = [line for line in lines if line.startswith("PassEnvironment=")]
    expected_pass = [] if unit.provider_env_key is None else [f"PassEnvironment={unit.provider_env_key}"]
    if pass_lines != expected_pass:
        raise ServiceError("installed service unit provider environment key drifted")
    profile_lines = [line for line in lines if line.startswith("# Codex-Flow-Profile-SHA256=")]
    expected_profile = [] if unit.profile_sha256 is None else [f"# Codex-Flow-Profile-SHA256={unit.profile_sha256}"]
    if allow_profile_identity_update:
        if len(profile_lines) != len(expected_profile) or any(
            re.fullmatch(r"# Codex-Flow-Profile-SHA256=[0-9a-f]{64}", line) is None for line in profile_lines
        ):
            raise ServiceError("installed service unit profile identity is malformed")
        profile_positions = [
            index for index, line in enumerate(lines) if line.startswith("# Codex-Flow-Profile-SHA256=")
        ]
        expected_positions = [
            index for index, line in enumerate(expected_lines) if line.startswith("# Codex-Flow-Profile-SHA256=")
        ]
        if profile_positions != expected_positions:
            raise ServiceError("installed service unit profile identity drifted")
        installed_without_profile = [line for line in lines if not line.startswith("# Codex-Flow-Profile-SHA256=")]
        expected_without_profile = [
            line for line in expected_lines if not line.startswith("# Codex-Flow-Profile-SHA256=")
        ]
        if installed_without_profile != expected_without_profile:
            raise ServiceError("installed service unit content drifted")
    elif profile_lines != expected_profile:
        raise ServiceError("installed service unit profile identity drifted")
    installed_digest = hashlib.sha256(raw).hexdigest()
    if not allow_profile_identity_update and (
        raw != expected or installed_digest != hashlib.sha256(expected).hexdigest()
    ):
        raise ServiceError("installed service unit content drifted")
    return installed_digest


def start_with_credential(
    unit: ServiceUnit,
    *,
    provider_env_key: str | None = None,
    profile_sha256: str | None = None,
    config_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> dict[str, object]:
    """Import one volatile credential by name and start the exact unit.

    Subprocess arguments contain only the environment key name; the value is
    read by the manager from the invoking environment and is never persisted
    or returned.
    """

    key, value = _require_profile_credential(
        unit,
        provider_env_key=provider_env_key,
        profile_sha256=profile_sha256,
        environment=environment,
    )
    _validate_installed_unit(
        unit,
        config_home=config_home,
        expected_profile_sha256=profile_sha256,
        credential_value=value,
    )
    imported = False
    try:
        imported = True
        result = runner(("systemctl", "--user", "import-environment", key), check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise ServiceStartFailed("user-manager credential import failed")
        result = runner(("systemctl", "--user", "start", unit.unit_name), check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise ServiceStartFailed("user service start failed")
    except BaseException as exc:
        if imported:
            try:
                runner(("systemctl", "--user", "unset-environment", key), check=False, capture_output=True, text=True)
            except BaseException:
                pass
        if isinstance(exc, OSError):
            raise ServiceStartFailed("user service lifecycle command failed") from None
        raise
    return {"started": True, "unit": unit.unit_name, "provider_env_key": key}


def refresh_with_credential(
    unit: ServiceUnit,
    *,
    provider_env_key: str | None = None,
    profile_sha256: str | None = None,
    native_compatibility_sha256: str | None = None,
    config_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    runner=subprocess.run,
    ledger: Ledger | None = None,
    deadline_seconds: float = 30.0,
    identity_reader: Callable[[int], str] = process_birth_identity,
    process_is_live: ProcessLiveness | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    shutdown_sender: RefreshShutdown = _send_authenticated_shutdown,
) -> dict[str, object]:
    """Refresh one unit through a fenced, authenticated harness handoff.

    The existing harness_authority.requested_shutdown bit is the durable
    fence. It stays set on every failure; the replacement harness clears it
    only as part of its normal atomic lease acquisition.
    """

    if deadline_seconds <= 0:
        raise ValueError("harness refresh deadline must be positive")
    if native_compatibility_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", native_compatibility_sha256) is None:
        raise ValueError("native compatibility identity is invalid")
    unit_path_value = _assert_existing_unit(unit, config_home=config_home)
    key, value = _require_profile_credential(
        unit,
        provider_env_key=provider_env_key,
        profile_sha256=profile_sha256,
        environment=environment,
    )
    owned_ledger = ledger
    migration_required = False
    ledger_path: Path | None = None
    if owned_ledger is None:
        ledger_path = unit.state_root / ".codex-flow" / "workflow.db"
        if not ledger_path.is_file():
            raise ServiceRefreshFailed("harness ledger is unavailable")
        try:
            compatibility = ledger_schema_compatibility(ledger_path)
            migration_required = compatibility.get("ledger_schema_version") == 18
            if compatibility.get("migration_required") and not migration_required:
                raise ServiceRefreshFailed("service refresh only supports the installed schema-v18 predecessor handoff")
            if migration_required:
                # Keep the predecessor schema intact while the old service is
                # fenced and stopped.  The migrating opener is created only
                # below, after that handoff has been observed.
                owned_ledger = Ledger(ledger_path, allow_legacy=True)
            else:
                owned_ledger = Ledger(ledger_path)
        except LedgerError as exc:
            raise ServiceRefreshFailed("harness ledger cannot be opened") from exc
    elif isinstance(ledger, Ledger):
        try:
            migration_required = int(ledger.schema_version) == 18
            if ledger.schema_version < CURRENT_SCHEMA_VERSION and not migration_required:
                raise ServiceRefreshFailed("service refresh only supports the installed schema-v18 predecessor handoff")
            ledger_path = ledger.path
        except LedgerError as exc:
            raise ServiceRefreshFailed("harness ledger cannot be inspected") from exc

    if migration_required:
        # The replacement unit cannot validate an installed v18 predecessor:
        # first prove the exact old command/description, then replace it only
        # after the predecessor has been fenced and stopped.
        _validate_installed_unit(
            _legacy_supervisor_unit(unit),
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
            allow_profile_identity_update=True,
        )
    else:
        _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
            allow_profile_identity_update=True,
        )

    assert owned_ledger is not None
    close_ledger = ledger is None or migration_required
    live_checker = process_is_live or (
        lambda pid, birth_identity: _exact_process_is_live(pid, birth_identity, identity_reader)
    )
    deadline = clock() + deadline_seconds

    def remaining() -> float:
        value = deadline - clock()
        if value <= 0:
            raise ServiceRefreshFailed("harness refresh timed out")
        return value

    imported = False
    try:
        try:
            fence = (
                owned_ledger.arm_predecessor_refresh_fence()
                if migration_required
                else owned_ledger.arm_harness_refresh_fence()
            )
        except HarnessRefreshBlocked as exc:
            raise ServiceRefreshDeferred(str(exc)) from exc
        except LedgerError as exc:
            raise ServiceRefreshFailed("harness refresh fence could not be armed") from exc
        if fence is None:
            raise ServiceRefreshFailed("harness authority is unavailable")

        authority = cast(Mapping[str, object], fence)
        _authority_matches_unit(authority, unit)
        old_pid, old_birth_identity, old_epoch = _authority_identity(authority)
        predecessor_unit = _legacy_supervisor_unit(unit) if migration_required else unit
        socket_path = (
            unit.state_root / ".codex-flow" / "runtime" / ("supervisor.sock" if migration_required else "harness.sock")
        )
        old_process_live = live_checker(old_pid, old_birth_identity)
        if old_process_live:
            shutdown_sender(socket_path, remaining())
            # The authenticated shutdown may synchronously close the exact
            # old owner. Re-read its birth-bound liveness before entering the
            # wait loop so an already completed handoff does not incur an
            # artificial sleep or deadline edge.
            old_process_live = live_checker(old_pid, old_birth_identity)
        elif not (
            owned_ledger.predecessor_refresh_fenced() if migration_required else owned_ledger.harness_refresh_fenced()
        ):
            raise ServiceRefreshFailed("harness fence disappeared before shutdown")

        while old_process_live or _unit_is_active(predecessor_unit, runner=runner):
            remaining()
            sleeper(min(0.05, remaining()))
            old_process_live = live_checker(old_pid, old_birth_identity)

        if migration_required:
            # The old authority is now fenced and its process/unit are gone;
            # only this explicit handoff may mutate the schema identity.
            if ledger_path is None:
                raise ServiceRefreshFailed("legacy ledger path is unavailable")
            if (unit.state_root / ".codex-flow" / "runtime" / "supervisor.sock").exists():
                raise ServiceRefreshFailed("legacy supervisor socket remains after predecessor stop")
            owned_ledger.close()
            owned_ledger = Ledger(ledger_path, migrate=True)

        if profile_sha256 is not None and native_compatibility_sha256 is not None:
            owned_ledger.authorize_controller_profile_refresh(
                native_profile_sha256=profile_sha256,
                native_compatibility_sha256=native_compatibility_sha256,
            )

        install_unit(unit, config_home=config_home)
        if "supervisor" in unit.text.lower() or unit.runtime != "harness":
            raise ServiceRefreshFailed("replacement service unit retains a supervisor runtime alias")
        _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
        )
        if _run_manager(("daemon-reload",), runner=runner) != 0:
            raise ServiceRefreshFailed("user-manager daemon reload failed")
        if _run_manager(("import-environment", key), runner=runner) != 0:
            raise ServiceRefreshFailed("user-manager credential import failed")
        imported = True
        if _run_manager(("start", unit.unit_name), runner=runner) != 0:
            raise ServiceRefreshFailed("user service replacement start failed")

        while True:
            remaining()
            replacement = owned_ledger.harness_authority()
            if _unit_is_active(unit, runner=runner) and _replacement_is_healthy(
                replacement,
                unit=unit,
                old_pid=old_pid,
                old_birth_identity=old_birth_identity,
                old_epoch=old_epoch,
                process_is_live=live_checker,
            ):
                assert replacement is not None
                return {
                    "refreshed": True,
                    "unit": unit.unit_name,
                    "provider_env_key": key,
                    "old_epoch": old_epoch,
                    "new_epoch": int(replacement["epoch"]),
                    "unit_path": os.fspath(unit_path_value),
                }
            sleeper(min(0.05, remaining()))
    except BaseException as exc:
        if imported:
            try:
                _run_manager(("unset-environment", key), runner=runner)
            except BaseException:
                pass
        if isinstance(exc, OSError):
            raise ServiceRefreshFailed("user service refresh command failed") from None
        if isinstance(exc, ServiceRefreshDeferred | ServiceRefreshFailed | ServiceError):
            raise
        if isinstance(exc, LedgerError):
            raise ServiceRefreshFailed("harness refresh ledger operation failed") from exc
        if isinstance(exc, Exception):
            raise ServiceRefreshFailed("harness refresh failed") from exc
        raise
    finally:
        if close_ledger and owned_ledger is not None:
            owned_ledger.close()


def uninstall_unit(*, repository_root: Path, config_home: Path | None = None) -> None:
    path = unit_path(config_home=config_home, repository_root=repository_root.resolve())
    _assert_no_symlink_ancestors(path)
    if path.is_symlink():
        raise ServiceError("service unit path is a symlink")
    if path.exists():
        path.unlink()


__all__ = [
    "CredentialUnavailable",
    "ServiceError",
    "ServiceRefreshDeferred",
    "ServiceRefreshFailed",
    "ServiceStartFailed",
    "ServiceUnit",
    "generate_unit",
    "install_unit",
    "process_birth_identity",
    "refresh_with_credential",
    "start_with_credential",
    "systemctl_user",
    "uninstall_unit",
    "unit_name",
    "unit_path",
]
