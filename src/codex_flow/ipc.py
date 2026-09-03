"""Small authenticated local IPC protocol for the detached supervisor."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from .domain import strict_json_loads

MAX_FRAME_BYTES = 65_536
_HEADER = struct.Struct("!I")


class IpcError(RuntimeError):
    """A local protocol or endpoint safety check failed."""

    def __init__(self, message: str, *, reason_code: IpcReasonCode | str | None = None) -> None:
        self.reason_code = reason_code.value if isinstance(reason_code, IpcReasonCode) else reason_code
        super().__init__(message)


class IpcReasonCode(str, Enum):
    """Stable, sanitized reasons for bounded response/protocol failures."""

    REQUEST_NOT_JSON = "request_not_json"
    RESPONSE_NOT_JSON = "response_not_json"
    FRAME_OVERSIZED = "frame_oversized"
    RESPONSE_TOO_LARGE = "response_too_large"


class IpcTransportError(IpcError):
    """A transient local socket failure prevented request/ack delivery."""


def encode_frame(value: Mapping[str, object]) -> bytes:
    if not isinstance(value, Mapping):
        raise IpcError("IPC request must be an object")
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IpcError("IPC request is not strict JSON", reason_code=IpcReasonCode.REQUEST_NOT_JSON) from exc
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise IpcError("IPC frame exceeds the bounded payload limit", reason_code=IpcReasonCode.FRAME_OVERSIZED)
    return _HEADER.pack(len(payload)) + payload


def recv_exact(connection: socket.socket, size: int) -> bytes:
    if size < 0 or size > MAX_FRAME_BYTES + _HEADER.size:
        raise IpcError("IPC frame length is invalid", reason_code=IpcReasonCode.FRAME_OVERSIZED)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise IpcError("IPC peer closed a fragmented frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame(connection: socket.socket) -> dict[str, object]:
    header = recv_exact(connection, _HEADER.size)
    size = _HEADER.unpack(header)[0]
    if size == 0 or size > MAX_FRAME_BYTES:
        raise IpcError("IPC frame exceeds the bounded payload limit", reason_code=IpcReasonCode.FRAME_OVERSIZED)
    raw = recv_exact(connection, size)
    try:
        decoded = strict_json_loads(raw, max_bytes=MAX_FRAME_BYTES)
    except ValueError as exc:
        raise IpcError("IPC frame is not strict UTF-8 JSON", reason_code=IpcReasonCode.RESPONSE_NOT_JSON) from exc
    if not isinstance(decoded, dict):
        raise IpcError("IPC frame root must be an object")
    return decoded


def peer_uid(connection: socket.socket) -> int | None:
    """Return Linux SO_PEERCRED uid when the platform exposes it."""

    try:
        peercred = socket.SO_PEERCRED
    except AttributeError:
        return None
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    except OSError as exc:
        raise IpcError("unable to verify local IPC peer credentials") from exc


def ensure_runtime_dir(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    # Walk the lexical ancestor chain without resolving links.  A symlink in
    # any component would let an attacker redirect the supervisor endpoint.
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise IpcError("supervisor runtime directory cannot contain symlinks")
        if not stat.S_ISDIR(metadata.st_mode):
            raise IpcError("supervisor runtime path contains a non-directory")
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise IpcError("supervisor runtime directory is not a private real directory")
    return path


def send_request(socket_path: Path, request: Mapping[str, object], *, timeout: float = 5.0) -> dict[str, object]:
    socket_path = Path(socket_path)
    ensure_runtime_dir(socket_path.parent)
    if socket_path.is_symlink() or not socket_path.exists():
        raise IpcError("supervisor IPC socket is unavailable")
    metadata = os.lstat(socket_path)
    if not stat.S_ISSOCK(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise IpcError("supervisor IPC socket has unsafe permissions")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(os.fspath(socket_path))
            connection.sendall(encode_frame(request))
        except OSError as exc:
            # Endpoint validation and frame encoding remain ``IpcError``
            # failures.  Only socket delivery failures are retryable by a
            # result-submit caller.
            raise IpcTransportError("IPC request transport failed") from exc
        try:
            return decode_frame(connection)
        except IpcError as exc:
            # A peer that committed a request and then disappeared before its
            # acknowledgement is indistinguishable from a lost local ack at
            # this boundary.  Malformed/oversized frames remain protocol
            # failures and are deliberately not converted.
            if "fragmented frame" in str(exc):
                raise IpcTransportError("IPC acknowledgement was lost") from exc
            raise
        except OSError as exc:
            raise IpcTransportError("IPC acknowledgement transport failed") from exc


__all__ = [
    "MAX_FRAME_BYTES",
    "IpcError",
    "IpcReasonCode",
    "IpcTransportError",
    "decode_frame",
    "encode_frame",
    "ensure_runtime_dir",
    "peer_uid",
    "send_request",
]
