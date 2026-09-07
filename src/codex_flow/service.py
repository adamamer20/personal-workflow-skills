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
    stdout: str


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


def _manager_result(
    arguments: tuple[str, ...],
    *,
    runner: Callable[..., object],
) -> _CommandResult:
    try:
        result = runner(("systemctl", "--user", *arguments), check=False, capture_output=True, text=True)
    except OSError as exc:
        operation = arguments[0] if arguments else "operation"
        raise ServiceRefreshFailed(f"user service {operation} command failed") from exc
    operation = arguments[0] if arguments else "operation"
    _manager_returncode(result, operation=operation)
    if not isinstance(result, _CommandResult) or not isinstance(result.stdout, str):
        raise ServiceRefreshFailed(f"user service {operation} returned no valid output")
    return result


def _run_manager(arguments: tuple[str, ...], *, runner: Callable[..., object]) -> int:
    return _manager_result(arguments, runner=runner).returncode


def _assert_unit_stopped(unit: ServiceUnit, *, runner: Callable[..., object]) -> None:
    result = _manager_result(
        ("show", unit.unit_name, "--property=ActiveState", "--property=MainPID", "--no-pager"), runner=runner
    )
    if result.returncode != 0:
        raise ServiceRefreshFailed("user service stopped-state query failed")
    raw = result.stdout
    if not raw.isascii() or len(raw) > 256:
        raise ServiceRefreshFailed("user service stopped-state response is malformed")
    lines = raw.removesuffix("\n").split("\n")
    properties: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if (
            separator != "="
            or key not in {"ActiveState", "MainPID"}
            or key in properties
            or not value
            or not value.isascii()
            or not all(character.isalnum() or character == "-" for character in value)
        ):
            raise ServiceRefreshFailed("user service stopped-state response is malformed")
        properties[key] = value
    if len(lines) != 2 or properties.keys() != {"ActiveState", "MainPID"}:
        raise ServiceRefreshFailed("user service stopped-state response is malformed")
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", properties["MainPID"]) is None:
        raise ServiceRefreshFailed("user service stopped-state PID is malformed")
    if properties["ActiveState"] != "inactive" or properties["MainPID"] != "0":
        raise ServiceRefreshDeferred("user service is not proven stopped")


def _unit_is_active(unit: ServiceUnit, *, runner: Callable[..., object]) -> bool:
    status = _run_manager(("is-active", "--quiet", unit.unit_name), runner=runner)
    if status == 0:
        return True
    # Negative activity is only a wait hint. Stopped-owner effects require the
    # joint ActiveState/MainPID observation in _assert_unit_stopped.
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


def _authority_shutdown_bit(authority: Mapping[str, object]) -> int:
    value = authority.get("requested_shutdown")
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
        raise ServiceRefreshFailed("harness authority shutdown fence is malformed")
    return value


def _authority_matches_unit(authority: Mapping[str, object], unit: ServiceUnit) -> None:
    for field, expected in (
        ("repository_root", os.fspath(unit.repository_root.resolve())),
        ("state_root", os.fspath(unit.state_root.resolve())),
        ("version", unit.version),
    ):
        if authority.get(field) != expected:
            raise ServiceRefreshFailed(f"harness authority {field} does not match the repository unit")


def _authority_stable_identity(authority: Mapping[str, object]) -> tuple[object, ...]:
    """Return the authority facts that cannot change during one handoff."""

    fields = (
        "repository_root",
        "state_root",
        "epoch",
        "owner_nonce_sha256",
        "pid",
        "process_birth_identity",
        "executable_digest",
        "version",
        "requested_shutdown",
    )
    values = tuple(authority.get(field) for field in fields)
    if any(value is None for value in values):
        raise ServiceRefreshFailed("harness authority identity is incomplete")
    owner_nonce_sha256 = values[3]
    executable_digest = values[6]
    if not isinstance(owner_nonce_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", owner_nonce_sha256) is None:
        raise ServiceRefreshFailed("harness authority owner identity is malformed")
    if not isinstance(executable_digest, str) or re.fullmatch(r"[0-9a-f]{64}", executable_digest) is None:
        raise ServiceRefreshFailed("harness authority executable identity is malformed")
    _authority_shutdown_bit(authority)
    return values


def _stable_regular_file(
    path: Path, *, label: str, reject_symlink: bool = True, max_bytes: int | None = 1_048_576
) -> tuple[bytes, int]:
    """Read one executable while proving its path and inode stayed stable."""

    if not path.is_absolute():
        raise ServiceRefreshFailed(f"{label} path is not absolute")
    _assert_no_symlink_ancestors(path)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if reject_symlink:
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ServiceRefreshFailed(f"{label} is unavailable") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not (before.st_mode & 0o111):
            raise ServiceRefreshFailed(f"{label} is not a single-link executable file")
        read_limit = before.st_size + 1 if max_bytes is None else max_bytes + 1
        first = os.read(fd, read_limit)
        os.lseek(fd, 0, os.SEEK_SET)
        second = os.read(fd, read_limit)
        after = os.fstat(fd)
        if (
            first != second
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
        ):
            raise ServiceRefreshFailed(f"{label} changed while being hashed")
        if max_bytes is not None and len(first) > max_bytes:
            raise ServiceRefreshFailed(f"{label} is oversized")
        if reject_symlink:
            try:
                linked = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ServiceRefreshFailed(f"{label} path is unavailable") from exc
            if (
                stat.S_ISLNK(linked.st_mode)
                or linked.st_nlink != 1
                or (linked.st_dev, linked.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise ServiceRefreshFailed(f"{label} path changed while being hashed")
        return first, stat.S_IMODE(after.st_mode)
    finally:
        os.close(fd)


def _installed_command_identity(unit: ServiceUnit) -> tuple[str, Path, str]:
    """Bind the installed console launcher to its direct Python interpreter."""

    launcher_raw, _launcher_mode = _stable_regular_file(unit.executable, label="installed service launcher")
    first_line, separator, _rest = launcher_raw.partition(b"\n")
    if not separator or not first_line.startswith(b"#!"):
        raise ServiceRefreshFailed("installed service launcher has no direct Python shebang")
    try:
        shebang = first_line[2:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ServiceRefreshFailed("installed service launcher shebang is malformed") from exc
    if shebang.startswith(" "):
        shebang = shebang[1:]
    if (
        not shebang.startswith("/")
        or not shebang
        or any(character.isspace() or ord(character) < 0x20 for character in shebang)
        or not Path(shebang).name.startswith("python")
    ):
        raise ServiceRefreshFailed("installed service launcher must use a direct absolute Python shebang")
    try:
        interpreter = Path(shebang).resolve(strict=True)
    except OSError as exc:
        raise ServiceRefreshFailed("installed service launcher interpreter is unavailable") from exc
    interpreter_raw, _interpreter_mode = _stable_regular_file(
        interpreter, label="installed service interpreter", reject_symlink=False, max_bytes=None
    )
    return hashlib.sha256(launcher_raw).hexdigest(), interpreter, hashlib.sha256(interpreter_raw).hexdigest()


def _process_executable_path(pid: int) -> Path:
    """Resolve Linux's executable identity for one live process."""

    raw = os.readlink(f"/proc/{pid}/exe")
    if not raw or raw.endswith(" (deleted)"):
        raise OSError("process executable path is unavailable")
    return Path(raw).resolve(strict=True)


def _process_matches_interpreter(
    pid: int,
    birth_identity: str,
    interpreter: Path,
    *,
    identity_reader: Callable[[int], str],
) -> bool:
    """Read /proc/exe only across two equal birth observations."""

    try:
        before = identity_reader(pid)
        if before != birth_identity:
            return False
        executable = _process_executable_path(pid)
        after = identity_reader(pid)
    except (OSError, ValueError):
        return False
    return before == after == birth_identity and executable == interpreter


def _prove_stopped_current_owner(
    ledger: Ledger,
    *,
    unit: ServiceUnit,
    config_home: Path | None,
    profile_sha256: str | None,
    credential_value: str,
    expected_authority: Mapping[str, object],
    expected_launcher_identity: tuple[str, Path, str],
    runner: Callable[..., object],
    process_is_live: ProcessLiveness,
    allow_profile_identity_update: bool = True,
) -> dict[str, object]:
    """Prove one unchanged fenced owner is stopped before a lifecycle edge."""

    try:
        inspection = ledger.refresh_state_inspection()
    except LedgerError as exc:
        raise ServiceRefreshFailed("harness refresh state is not canonical") from exc
    authority_value = inspection.get("authority")
    if not isinstance(authority_value, Mapping):
        raise ServiceRefreshFailed("harness authority is unavailable")
    _authority_matches_unit(authority_value, unit)
    if _authority_stable_identity(authority_value) != _authority_stable_identity(expected_authority):
        raise ServiceRefreshFailed("harness authority changed while the predecessor was stopping")
    if _authority_shutdown_bit(authority_value) != 1:
        raise ServiceRefreshFailed("harness refresh fence disappeared while the predecessor was stopping")
    for name in (
        "active_dispatches",
        "live_worker_leases",
        "active_controller_generations",
        "live_controller_claims",
    ):
        count = inspection.get(name)
        if isinstance(count, bool) or not isinstance(count, int):
            raise ServiceRefreshFailed("harness refresh state contains an invalid activity count")
        if count:
            raise ServiceRefreshDeferred("harness refresh deferred while a child or controller claim is active")
    old_pid, old_birth_identity, _old_epoch = _authority_identity(authority_value)
    if process_is_live(old_pid, old_birth_identity):
        raise ServiceRefreshDeferred("harness predecessor process is still live")
    _assert_unit_stopped(unit, runner=runner)
    _validate_installed_unit(
        unit,
        config_home=config_home,
        expected_profile_sha256=profile_sha256,
        credential_value=credential_value,
        allow_profile_identity_update=allow_profile_identity_update,
    )
    if _installed_command_identity(unit) != expected_launcher_identity:
        raise ServiceRefreshFailed("installed service launcher changed while the predecessor was stopping")
    runtime = unit.state_root / ".codex-flow" / "runtime"
    if any(os.path.lexists(runtime / socket_name) for socket_name in ("harness.sock", "supervisor.sock")):
        raise ServiceRefreshFailed("harness refresh socket entry remains")
    return dict(authority_value)


def _replacement_is_healthy(
    authority: Mapping[str, object] | None,
    *,
    unit: ServiceUnit,
    old_pid: int,
    old_birth_identity: str,
    old_epoch: int,
    process_is_live: ProcessLiveness,
    identity_reader: Callable[[int], str],
    interpreter: Path,
    interpreter_digest: str,
) -> bool:
    if authority is None:
        return False
    _authority_matches_unit(authority, unit)
    try:
        pid = authority["pid"]
        birth_identity = authority["process_birth_identity"]
        epoch = authority["epoch"]
    except KeyError as exc:
        raise ServiceRefreshFailed("replacement harness authority is incomplete") from exc
    requested_shutdown = _authority_shutdown_bit(authority)
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
    if authority.get("executable_digest") != interpreter_digest:
        return False
    return process_is_live(pid, birth_identity) and _process_matches_interpreter(
        pid,
        birth_identity,
        interpreter,
        identity_reader=identity_reader,
    )


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
    expected_exec = (
        f"ExecStart={_unit_arg(unit.executable)} harness run --foreground --state-root {_unit_arg(unit.state_root)}"
    )
    old_lines: list[str] = []
    description_replaced = False
    exec_replaced = False
    for line in unit.text.splitlines(keepends=True):
        if line.startswith("Description=Codex Flow harness ("):
            line = line.replace("Description=Codex Flow harness ", "Description=Codex Flow supervisor ", 1)
            description_replaced = True
        elif line.startswith("ExecStart="):
            if line.rstrip("\n") != expected_exec:
                raise ServiceError("replacement service command is not the harness template")
            line = line.replace(" harness run --foreground", " supervisor run --foreground", 1)
            exec_replaced = True
        old_lines.append(line)
    old_text = "".join(old_lines)
    if not description_replaced or not exec_replaced or old_text == unit.text:
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


def _read_installed_unit(path: Path) -> tuple[bytes, str, int]:
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
        mode = stat.S_IMODE(metadata.st_mode)
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
    return raw, text, mode


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
    raw, text, _mode = _read_installed_unit(path)
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
    predecessor_schema_version: int | None = None
    migration_required = False
    legacy_topology_migration = False
    interrupted_recovery = False
    ledger_path: Path | None = None
    authority_value: Mapping[str, object] | None = None
    live_checker = process_is_live or (
        lambda pid, birth_identity: _exact_process_is_live(pid, birth_identity, identity_reader)
    )
    if owned_ledger is None:
        ledger_path = unit.state_root / ".codex-flow" / "workflow.db"
        if not ledger_path.is_file():
            raise ServiceRefreshFailed("harness ledger is unavailable")
        try:
            compatibility = ledger_schema_compatibility(ledger_path)
            raw_schema_version = compatibility.get("ledger_schema_version")
            if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
                raise ServiceRefreshFailed("harness ledger schema identity is unavailable")
            predecessor_schema_version = raw_schema_version
            migration_required = bool(compatibility.get("migration_required"))
            if migration_required and predecessor_schema_version not in {18, 19, 20}:
                raise ServiceRefreshFailed("service refresh only supports schema-v18/v19/v20 predecessor handoff")
            legacy_topology_migration = predecessor_schema_version == 18
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
            predecessor_schema_version = int(ledger.schema_version)
            migration_required = predecessor_schema_version < int(CURRENT_SCHEMA_VERSION)
            if migration_required and predecessor_schema_version not in {18, 19, 20}:
                raise ServiceRefreshFailed("service refresh only supports schema-v18/v19/v20 predecessor handoff")
            legacy_topology_migration = predecessor_schema_version == 18
            ledger_path = ledger.path
        except LedgerError as exc:
            raise ServiceRefreshFailed("harness ledger cannot be inspected") from exc

    if not migration_required:
        # A real interrupted post-migration handoff has v19 authority and the
        # exact stopped v18 unit. Recognize it before normal current-harness
        # validation, without reconstructing or mutating the predecessor.
        try:
            _validate_installed_unit(
                _legacy_supervisor_unit(unit),
                config_home=config_home,
                expected_profile_sha256=profile_sha256,
                credential_value=value,
                allow_profile_identity_update=True,
            )
        except ServiceError as legacy_error:
            try:
                _validate_installed_unit(
                    unit,
                    config_home=config_home,
                    expected_profile_sha256=profile_sha256,
                    credential_value=value,
                    allow_profile_identity_update=True,
                )
            except ServiceError:
                raise legacy_error from None
        else:
            interrupted_recovery = True
            try:
                inspection = owned_ledger.refresh_state_inspection()  # type: ignore[union-attr]
            except LedgerError as exc:
                raise ServiceRefreshFailed("harness refresh state is not canonical schema-v19") from exc
            authority_value = inspection.get("authority")
            if not isinstance(authority_value, Mapping):
                raise ServiceRefreshFailed("harness authority is unavailable")
            if _authority_shutdown_bit(authority_value) != 1:
                raise ServiceRefreshFailed("interrupted refresh predecessor is not fenced")
            if any(
                int(inspection.get(name, 0)) != 0
                for name in (
                    "active_dispatches",
                    "live_worker_leases",
                    "active_controller_generations",
                    "live_controller_claims",
                )
            ):
                raise ServiceRefreshDeferred("interrupted refresh still has active children or claims")
            old_pid, old_birth_identity, old_epoch = _authority_identity(authority_value)
            if live_checker(old_pid, old_birth_identity):
                raise ServiceRefreshDeferred("interrupted refresh predecessor process is still live")
            _assert_unit_stopped(_legacy_supervisor_unit(unit), runner=runner)
            runtime = unit.state_root / ".codex-flow" / "runtime"
            for socket_name in ("supervisor.sock", "harness.sock"):
                if os.path.lexists(runtime / socket_name):
                    raise ServiceRefreshFailed("interrupted refresh socket entry remains")

    if legacy_topology_migration:
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
    elif not interrupted_recovery:
        predecessor_unit_digest = _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
            allow_profile_identity_update=True,
        )

    predecessor_unit_snapshot: tuple[bytes, str, int] | None = None
    if not (legacy_topology_migration or interrupted_recovery):
        predecessor_unit_snapshot = _read_installed_unit(unit_path_value)
        if hashlib.sha256(predecessor_unit_snapshot[0]).hexdigest() != predecessor_unit_digest:
            raise ServiceRefreshFailed("installed predecessor unit changed during capture")
        predecessor_profile = next(
            (
                line
                for line in predecessor_unit_snapshot[1].splitlines()
                if line.startswith("# Codex-Flow-Profile-SHA256=")
            ),
            None,
        )
        target_profile = None if profile_sha256 is None else f"# Codex-Flow-Profile-SHA256={profile_sha256}"
        if predecessor_profile != target_profile and native_compatibility_sha256 is None:
            raise ServiceRefreshFailed("profile rotation requires explicit native compatibility identity")

    def assert_predecessor_unit_unchanged() -> None:
        if predecessor_unit_snapshot is not None:
            if _read_installed_unit(unit_path_value) != predecessor_unit_snapshot:
                raise ServiceRefreshFailed("installed predecessor unit changed during refresh")

    installed_launcher_identity = _installed_command_identity(unit)

    legacy_unit_raw: bytes | None = None
    legacy_unit_mode: int | None = None
    interrupted_unit_staged = False
    if legacy_topology_migration or interrupted_recovery:
        # Keep an exact rollback image until the v18 ledger has crossed its
        # migration fence. Installing the replacement before migration means
        # an install failure leaves the fenced v18 pair retryable, never a v19
        # ledger paired only with a stopped predecessor unit. The same image
        # protects an interrupted v19 handoff while its replacement unit is
        # being staged or health-checked. Same-topology v19/v20 migrations
        # retain the current unit and therefore have no legacy rollback image.
        legacy_unit_raw, _legacy_unit_text, legacy_unit_mode = _read_installed_unit(unit_path_value)

    def restore_legacy_unit(*, replacement_death_proven: bool = False) -> None:
        if legacy_unit_raw is None or legacy_unit_mode is None:
            return
        if post_start_or_possible_replacement and not replacement_death_proven:
            raise ServiceRefreshFailed("legacy service unit rollback is unsafe after replacement start")
        try:
            _assert_no_symlink_ancestors(unit_path_value)
            metadata = unit_path_value.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("service unit path is not a private regular file")
            unit_path_value.write_bytes(legacy_unit_raw)
            os.chmod(unit_path_value, legacy_unit_mode)
        except OSError as restore_error:
            raise ServiceRefreshFailed("legacy service unit rollback failed") from restore_error

    def restore_legacy_pair() -> None:
        if ledger_path is None:
            raise ServiceRefreshFailed("legacy ledger rollback cannot be verified")
        try:
            compatibility = ledger_schema_compatibility(ledger_path)
        except LedgerError as rollback_error:
            raise ServiceRefreshFailed("legacy ledger rollback cannot be verified") from rollback_error
        if compatibility.get("ledger_schema_version") != 18:
            raise ServiceRefreshFailed("legacy ledger rollback did not restore schema v18")
        restore_legacy_unit()

    assert owned_ledger is not None
    close_ledger = ledger is None or migration_required
    deadline = clock() + deadline_seconds

    def remaining() -> float:
        value = deadline - clock()
        if value <= 0:
            raise ServiceRefreshFailed("harness refresh timed out")
        return value

    imported = False
    # Once the replacement manager has been asked to start, the installed unit
    # may already be executing even when systemctl returns an error.  This
    # guard is intentionally monotonic: no later authority parsing or health
    # failure can make direct restoration of the legacy unit safe again.
    post_start_or_possible_replacement = False

    def mark_possible_replacement() -> None:
        nonlocal post_start_or_possible_replacement
        post_start_or_possible_replacement = True

    replacement_authority_seen = False
    replacement_identity: tuple[int, str, int] | None = None

    def rollback_failed_replacement(
        replacement: Mapping[str, object],
        *,
        old_pid: int,
        old_birth_identity: str,
        old_epoch: int,
    ) -> None:
        """Fence, stop, and roll back one unhealthy acquired replacement.

        The replacement row is the only authority for the process identity.
        A second harness fence is deliberately taken only after that row has
        been validated; no unit bytes are restored until the same fenced row,
        exact unit inactivity, and birth-bound process death are all proven.
        """

        if not isinstance(replacement, Mapping):
            raise ServiceRefreshFailed("replacement harness authority is ambiguous")
        _authority_matches_unit(replacement, unit)
        replacement_pid, replacement_birth, replacement_epoch = _authority_identity(replacement)
        if replacement_epoch <= old_epoch:
            raise ServiceRefreshFailed("replacement harness authority epoch did not advance")
        if replacement_pid == old_pid and replacement_birth == old_birth_identity:
            raise ServiceRefreshFailed("replacement harness authority reused the predecessor identity")
        shutdown_bit = _authority_shutdown_bit(replacement)
        identity = (replacement_pid, replacement_birth, replacement_epoch)
        if replacement_identity is not None and identity != replacement_identity:
            raise ServiceRefreshFailed("replacement harness authority identity drifted")

        _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
        )

        # A replacement which already carries the durable shutdown fence is
        # already isolated.  Refencing it would be a second lifecycle
        # mutation (and can race its own shutdown), so reuse that exact row.
        if shutdown_bit == 1:
            fenced: object = replacement
        else:
            try:
                fenced = owned_ledger.arm_harness_refresh_fence()
            except HarnessRefreshBlocked as exc:
                raise ServiceRefreshDeferred(str(exc)) from exc
            except LedgerError as exc:
                raise ServiceRefreshFailed("replacement harness refresh fence could not be armed") from exc
        if not isinstance(fenced, Mapping):
            raise ServiceRefreshFailed("replacement harness authority is unavailable")
        _authority_matches_unit(fenced, unit)
        fenced_pid, fenced_birth, fenced_epoch = _authority_identity(fenced)
        if (fenced_pid, fenced_birth, fenced_epoch) != identity or _authority_shutdown_bit(fenced) != 1:
            raise ServiceRefreshFailed("replacement harness authority changed while fencing")

        if _run_manager(("stop", unit.unit_name), runner=runner) != 0:
            raise ServiceRefreshFailed("user service replacement stop failed")

        while True:
            remaining()
            if not _unit_is_active(unit, runner=runner) and not live_checker(fenced_pid, fenced_birth):
                break
            sleeper(min(0.05, remaining()))

        _assert_unit_stopped(unit, runner=runner)
        current = owned_ledger.harness_authority()
        if not isinstance(current, Mapping):
            raise ServiceRefreshFailed("replacement harness authority disappeared after stop")
        _authority_matches_unit(current, unit)
        current_pid, current_birth, current_epoch = _authority_identity(current)
        if (current_pid, current_birth, current_epoch) != identity or _authority_shutdown_bit(current) != 1:
            raise ServiceRefreshFailed("replacement harness authority changed after stop")
        if _authority_stable_identity(current) != _authority_stable_identity(cast(Mapping[str, object], fenced)):
            raise ServiceRefreshFailed("replacement harness authority row changed after stop")

        if not (legacy_topology_migration or interrupted_recovery):
            _prove_stopped_current_owner(
                owned_ledger,
                unit=unit,
                config_home=config_home,
                profile_sha256=profile_sha256,
                credential_value=value,
                expected_authority=fenced,
                expected_launcher_identity=installed_launcher_identity,
                runner=runner,
                process_is_live=live_checker,
                allow_profile_identity_update=False,
            )
            return

        _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
        )

        # This is the only post-start restoration path.  The exact replacement
        # authority was fenced, the exact unit was stopped, and its
        # birth-bound process was proven dead above.
        restore_legacy_unit(replacement_death_proven=True)
        restored_raw, _restored_text, restored_mode = _read_installed_unit(unit_path_value)
        if restored_raw != legacy_unit_raw or restored_mode != legacy_unit_mode:
            raise ServiceRefreshFailed("legacy service unit rollback was not exact")

    def prove_no_replacement(
        *, old_pid: int, old_birth_identity: str, old_epoch: int, expected_authority: Mapping[str, object]
    ) -> bool:
        """Prove an unchanged fenced predecessor is the only remaining owner.

        This is deliberately positive evidence, not an absence inferred from
        a timeout: every observation must show the exact fenced predecessor,
        an inactive unit, a dead birth-bound PID and no runtime socket entry.
        Three observations are required with real monotonic spacing so a
        delayed replacement cannot be hidden by immediate legacy restoration.
        """

        observations: list[float] = []
        for index in range(3):
            remaining()
            observed_at = clock()
            if observations and observed_at - observations[-1] < 0.05:
                return False
            observations.append(observed_at)
            current = owned_ledger.harness_authority()
            if not isinstance(current, Mapping):
                return False
            try:
                _authority_matches_unit(current, unit)
                current_identity = _authority_identity(current)
                current_shutdown = _authority_shutdown_bit(current)
            except ServiceRefreshFailed:
                return False
            if current_identity != (old_pid, old_birth_identity, old_epoch) or current_shutdown != 1:
                return False
            if dict(current) != dict(expected_authority):
                return False
            try:
                _assert_unit_stopped(unit, runner=runner)
            except ServiceRefreshDeferred:
                return False
            if live_checker(old_pid, old_birth_identity):
                return False
            runtime = unit.state_root / ".codex-flow" / "runtime"
            if any(os.path.lexists(runtime / socket_name) for socket_name in ("supervisor.sock", "harness.sock")):
                return False
            if index < 2:
                remaining()
                sleeper(min(0.05, remaining()))

        # Re-read the authority immediately after the last observation.  A
        # replacement that acquired during the final interval is never hidden
        # behind the captured legacy bytes.
        remaining()
        final = owned_ledger.harness_authority()
        if not isinstance(final, Mapping):
            return False
        try:
            _authority_matches_unit(final, unit)
            final_identity = _authority_identity(final)
            final_shutdown = _authority_shutdown_bit(final)
        except ServiceRefreshFailed:
            return False
        if final_identity != (old_pid, old_birth_identity, old_epoch) or final_shutdown != 1:
            return False
        if dict(final) != dict(expected_authority):
            return False
        try:
            _assert_unit_stopped(unit, runner=runner)
        except ServiceRefreshDeferred:
            return False
        if live_checker(old_pid, old_birth_identity):
            return False
        runtime = unit.state_root / ".codex-flow" / "runtime"
        return not any(os.path.lexists(runtime / socket_name) for socket_name in ("supervisor.sock", "harness.sock"))

    try:
        try:
            if interrupted_recovery:
                if authority_value is None:
                    raise ServiceRefreshFailed("harness authority is unavailable")
                predecessor_authority = dict(authority_value)
            else:
                fence = (
                    owned_ledger.arm_predecessor_refresh_fence()
                    if migration_required
                    else owned_ledger.arm_harness_refresh_fence()
                )
                if fence is None:
                    raise ServiceRefreshFailed("harness authority is unavailable")
                predecessor_authority = dict(cast(Mapping[str, object], fence))
        except HarnessRefreshBlocked as exc:
            raise ServiceRefreshDeferred(str(exc)) from exc
        except LedgerError as exc:
            raise ServiceRefreshFailed("harness refresh fence could not be armed") from exc

        _authority_matches_unit(predecessor_authority, unit)
        _authority_stable_identity(predecessor_authority)
        old_pid, old_birth_identity, old_epoch = _authority_identity(predecessor_authority)
        legacy_topology = legacy_topology_migration or interrupted_recovery
        predecessor_unit = _legacy_supervisor_unit(unit) if legacy_topology else unit
        socket_path = (
            unit.state_root / ".codex-flow" / "runtime" / ("supervisor.sock" if legacy_topology else "harness.sock")
        )
        if os.path.lexists(socket_path):
            try:
                socket_metadata = os.lstat(socket_path)
            except OSError as exc:
                raise ServiceRefreshFailed("predecessor harness socket path is unavailable") from exc
            if stat.S_ISLNK(socket_metadata.st_mode):
                raise ServiceRefreshFailed("predecessor harness socket path must not be a symlink")
        old_process_live = False if interrupted_recovery else live_checker(old_pid, old_birth_identity)
        if old_process_live:
            shutdown_sender(socket_path, remaining())
            # The authenticated shutdown may synchronously close the exact
            # old owner. Re-read its birth-bound liveness before entering the
            # wait loop so an already completed handoff does not incur an
            # artificial sleep or deadline edge.
            old_process_live = live_checker(old_pid, old_birth_identity)
        elif not interrupted_recovery and not (
            owned_ledger.predecessor_refresh_fenced() if migration_required else owned_ledger.harness_refresh_fenced()
        ):
            raise ServiceRefreshFailed("harness fence disappeared before shutdown")

        while old_process_live or _unit_is_active(predecessor_unit, runner=runner):
            remaining()
            sleeper(min(0.05, remaining()))
            old_process_live = live_checker(old_pid, old_birth_identity)

        predecessor_authority = _prove_stopped_current_owner(
            owned_ledger,
            unit=predecessor_unit,
            config_home=config_home,
            profile_sha256=profile_sha256,
            credential_value=value,
            expected_authority=predecessor_authority,
            expected_launcher_identity=installed_launcher_identity,
            runner=runner,
            process_is_live=live_checker,
        )

        assert_predecessor_unit_unchanged()

        if migration_required:
            # The old authority is now fenced and its process/unit are gone.
            # Only the v18 supervisor topology needs a replacement unit staged
            # before its schema rename; v19/v20 already use the current unit
            # and keep those bytes in place while migrating forward.
            if ledger_path is None:
                raise ServiceRefreshFailed("legacy ledger path is unavailable")
            runtime = unit.state_root / ".codex-flow" / "runtime"
            if legacy_topology_migration:
                if os.path.lexists(runtime / "supervisor.sock"):
                    raise ServiceRefreshFailed("legacy supervisor socket remains after predecessor stop")
                if os.path.lexists(runtime / "harness.sock"):
                    raise ServiceRefreshFailed("unexpected harness socket remains after predecessor stop")
                try:
                    install_unit(unit, config_home=config_home)
                    _validate_installed_unit(
                        unit,
                        config_home=config_home,
                        expected_profile_sha256=profile_sha256,
                        credential_value=value,
                    )
                except BaseException:
                    restore_legacy_pair()
                    raise
            else:
                if os.path.lexists(runtime / "harness.sock"):
                    raise ServiceRefreshFailed("harness socket remains after predecessor stop")
                if os.path.lexists(runtime / "supervisor.sock"):
                    raise ServiceRefreshFailed("unexpected supervisor socket remains after predecessor stop")
                _validate_installed_unit(
                    unit,
                    config_home=config_home,
                    expected_profile_sha256=profile_sha256,
                    credential_value=value,
                    allow_profile_identity_update=True,
                )
                assert_predecessor_unit_unchanged()
            owned_ledger.close()
            try:
                owned_ledger = Ledger(
                    ledger_path,
                    migrate=True,
                    expected_refresh_authority=predecessor_authority,
                )
            except BaseException:
                # The predecessor fence is committed before migration. If
                # opening or migrating fails, v18 restores its exact legacy
                # pair; v19/v20 retain their current-schema unit and ledger
                # bytes without any topology downgrade.
                if legacy_topology_migration:
                    restore_legacy_pair()
                raise

        assert_predecessor_unit_unchanged()
        if profile_sha256 is not None and native_compatibility_sha256 is not None:
            owned_ledger.authorize_controller_profile_refresh(
                native_profile_sha256=profile_sha256,
                native_compatibility_sha256=native_compatibility_sha256,
            )

        if not (legacy_topology_migration or interrupted_recovery):
            predecessor_authority = _prove_stopped_current_owner(
                owned_ledger,
                unit=unit,
                config_home=config_home,
                profile_sha256=profile_sha256,
                credential_value=value,
                expected_authority=predecessor_authority,
                expected_launcher_identity=installed_launcher_identity,
                runner=runner,
                process_is_live=live_checker,
            )
            assert_predecessor_unit_unchanged()

        if not legacy_topology_migration:
            interrupted_unit_staged = interrupted_recovery
            install_unit(unit, config_home=config_home)
        if unit.runtime != "harness":
            raise ServiceRefreshFailed("replacement service unit runtime is not harness")
        _validate_installed_unit(
            unit,
            config_home=config_home,
            expected_profile_sha256=profile_sha256,
            credential_value=value,
        )
        target_unit_snapshot = _read_installed_unit(unit_path_value)
        if target_unit_snapshot[0] != unit.text.encode("utf-8"):
            raise ServiceRefreshFailed("installed target unit changed during capture")
        if _installed_command_identity(unit) != installed_launcher_identity:
            raise ServiceRefreshFailed("installed service launcher changed before replacement start")
        if _run_manager(("daemon-reload",), runner=runner) != 0:
            raise ServiceRefreshFailed("user-manager daemon reload failed")
        if _run_manager(("import-environment", key), runner=runner) != 0:
            raise ServiceRefreshFailed("user-manager credential import failed")
        imported = True
        predecessor_authority = _prove_stopped_current_owner(
            owned_ledger,
            unit=unit,
            config_home=config_home,
            profile_sha256=profile_sha256,
            credential_value=value,
            expected_authority=predecessor_authority,
            expected_launcher_identity=installed_launcher_identity,
            runner=runner,
            process_is_live=live_checker,
            allow_profile_identity_update=False,
        )
        if _read_installed_unit(unit_path_value) != target_unit_snapshot:
            raise ServiceRefreshFailed("installed target unit changed before replacement start")
        # Mark before invoking systemctl start.  A failed/timeout start is
        # still an uncertain replacement unless the bounded positive absence
        # proof below establishes that no replacement could have run.
        mark_possible_replacement()
        try:
            start_status = _run_manager(("start", unit.unit_name), runner=runner)
        except Exception:
            if interrupted_recovery and prove_no_replacement(
                old_pid=old_pid,
                old_birth_identity=old_birth_identity,
                old_epoch=old_epoch,
                expected_authority=predecessor_authority,
            ):
                restore_legacy_unit(replacement_death_proven=True)
            raise
        if start_status != 0:
            if interrupted_recovery and prove_no_replacement(
                old_pid=old_pid,
                old_birth_identity=old_birth_identity,
                old_epoch=old_epoch,
                expected_authority=predecessor_authority,
            ):
                restore_legacy_unit(replacement_death_proven=True)
            raise ServiceRefreshFailed("user service replacement start failed")

        while True:
            remaining()
            # The start boundary is already crossed before any authority row
            # (including requested_shutdown) is parsed.  This guard is
            # monotonic for every subsequent failure classification.
            mark_possible_replacement()
            replacement = owned_ledger.harness_authority()
            if replacement is None:
                replacement_authority_seen = True
                raise ServiceRefreshFailed("replacement harness authority is unavailable")
            if not isinstance(replacement, Mapping):
                replacement_authority_seen = True
                raise ServiceRefreshFailed("replacement harness authority is ambiguous")
            try:
                _authority_matches_unit(replacement, unit)
                replacement_pid, replacement_birth, replacement_epoch = _authority_identity(replacement)
            except ServiceRefreshFailed:
                replacement_authority_seen = True
                raise

            replacement_authority_seen = True
            identity = (replacement_pid, replacement_birth, replacement_epoch)
            # The shutdown fence is untrusted input.  Keep the guard set
            # before parsing it so malformed values cannot re-enable legacy
            # restore.  The exact unchanged fenced predecessor is handled by
            # a bounded positive absence proof; every other greater epoch is a
            # replacement, including one that arrived already fenced.
            shutdown_bit = _authority_shutdown_bit(replacement)
            if (
                replacement_epoch == old_epoch
                and replacement_pid == old_pid
                and replacement_birth == old_birth_identity
                and shutdown_bit == 1
            ):
                if interrupted_recovery and prove_no_replacement(
                    old_pid=old_pid,
                    old_birth_identity=old_birth_identity,
                    old_epoch=old_epoch,
                    expected_authority=predecessor_authority,
                ):
                    restore_legacy_unit(replacement_death_proven=True)
                    raise ServiceRefreshFailed("replacement harness authority did not acquire a newer epoch")
                # The predecessor is still the sole row, but complete
                # absence proof was not established.  Keep the current
                # bytes in place and continue only within the existing
                # deadline so a delayed replacement remains observable.
                remaining()
                sleeper(min(0.05, remaining()))
                continue

            # Only a row that has crossed the predecessor epoch is a
            # replacement identity.  The exact fenced predecessor may remain
            # observable while a legitimate replacement is still acquiring;
            # recording it here would falsely classify that later replacement
            # as identity drift.  Once acquisition begins, every subsequent
            # replacement observation must retain the same process/birth/epoch
            # tuple.
            if replacement_identity is None:
                replacement_identity = identity
            elif replacement_identity != identity:
                raise ServiceRefreshFailed("replacement harness authority identity drifted")

            if _installed_command_identity(unit) != installed_launcher_identity:
                raise ServiceRefreshFailed("installed service launcher changed during replacement health")
            healthy = (
                shutdown_bit == 0
                and _unit_is_active(unit, runner=runner)
                and _replacement_is_healthy(
                    replacement,
                    unit=unit,
                    old_pid=old_pid,
                    old_birth_identity=old_birth_identity,
                    old_epoch=old_epoch,
                    process_is_live=live_checker,
                    identity_reader=identity_reader,
                    interpreter=installed_launcher_identity[1],
                    interpreter_digest=installed_launcher_identity[2],
                )
            )
            if healthy:
                assert replacement is not None
                return {
                    "refreshed": True,
                    "unit": unit.unit_name,
                    "provider_env_key": key,
                    "old_epoch": old_epoch,
                    "new_epoch": int(replacement["epoch"]),
                    "unit_path": os.fspath(unit_path_value),
                }
            rollback_failed_replacement(
                replacement,
                old_pid=old_pid,
                old_birth_identity=old_birth_identity,
                old_epoch=old_epoch,
            )
            raise ServiceRefreshFailed("replacement harness health check failed")
    except BaseException as exc:
        if (
            interrupted_recovery
            and interrupted_unit_staged
            and not replacement_authority_seen
            and not post_start_or_possible_replacement
        ):
            try:
                restore_legacy_unit()
            except BaseException as restore_error:
                raise ServiceRefreshFailed("interrupted refresh legacy service unit rollback failed") from restore_error
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
