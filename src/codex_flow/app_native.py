"""Typed host-mediated boundary for visible Codex App workers.

The controller prepares data for one native task action, but it never calls a
Desktop socket or the headless SDK on this path.  The hosting app is the only
owner of native task creation and must bind the identity it actually receives.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from .contracts import ModelFacingResult
from .domain import DispatchId, JsonObject, ReasoningEffort, ThreadIdentity, freeze_json, thaw_json

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_MAX_PROMPT_BYTES: Final[int] = 65_536
_MAX_IDENTITY_BYTES: Final[int] = 512


class AppNativeError(ValueError):
    """An App-native request or durable fact violates the closed protocol."""


class HostingMode(str, Enum):
    SDK_HEADLESS = "sdk-headless"
    APP_NATIVE = "app-native"


class AppNativeState(str, Enum):
    PREPARED = "prepared"
    BOUND = "bound"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _bounded_identity(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > _MAX_IDENTITY_BYTES:
        raise AppNativeError(f"{label} must be a bounded non-empty string")
    return value


def claim_token_sha256(token: str) -> str:
    if not isinstance(token, str) or _TOKEN_PATTERN.fullmatch(token) is None:
        raise AppNativeError("claim token must be a lowercase 256-bit hexadecimal capability")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class HostIdentity:
    host_id: str
    thread_id: ThreadIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "host_id", _bounded_identity(self.host_id, label="host identity"))
        if not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))

    def to_json(self) -> JsonObject:
        return {"host_id": self.host_id, "thread_id": self.thread_id.id}


@dataclass(frozen=True, slots=True)
class AppNativeTaskAction:
    """One closed, non-blocking native task creation requested from the host."""

    schema_version: int
    dispatch_id: DispatchId
    claim_token: str
    bind_challenge: str
    model: str
    reasoning_effort: ReasoningEffort
    workspace_path: Path
    prompt: str
    output_schema: JsonObject
    action: str = "create_native_task"
    non_blocking: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AppNativeError("unsupported App-native action schema version")
        if self.action != "create_native_task" or self.non_blocking is not True:
            raise AppNativeError("App-native action must be one non-blocking native task creation")
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        claim_token_sha256(self.claim_token)
        claim_token_sha256(self.bind_challenge)
        _bounded_identity(self.model, label="model")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            object.__setattr__(self, "reasoning_effort", ReasoningEffort(self.reasoning_effort))
        try:
            workspace = self.workspace_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AppNativeError("workspace path must have an existing physical identity") from exc
        if not workspace.is_dir() or not workspace.is_absolute():
            raise AppNativeError("workspace path must be an absolute existing directory")
        object.__setattr__(self, "workspace_path", workspace)
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or len(self.prompt.encode("utf-8")) > _MAX_PROMPT_BYTES
        ):
            raise AppNativeError("native task prompt must be non-empty and at most 65536 bytes")
        try:
            schema = freeze_json(self.output_schema)
        except (TypeError, RecursionError) as exc:
            raise AppNativeError("native task output schema must be a JSON object") from exc
        if not isinstance(schema, Mapping):
            raise AppNativeError("native task output schema must be a JSON object")
        object.__setattr__(self, "output_schema", schema)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "non_blocking": self.non_blocking,
            "dispatch_id": str(self.dispatch_id),
            "claim_token": self.claim_token,
            "bind_challenge": self.bind_challenge,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort.value,
            "workspace_path": str(self.workspace_path),
            "prompt": self.prompt,
            "output_schema": thaw_json(self.output_schema),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> AppNativeTaskAction:
        expected = {
            "schema_version",
            "action",
            "non_blocking",
            "dispatch_id",
            "claim_token",
            "bind_challenge",
            "model",
            "reasoning_effort",
            "workspace_path",
            "prompt",
            "output_schema",
        }
        if set(value) != expected:
            raise AppNativeError(f"App-native action keys must be exactly {sorted(expected)!r}")
        version = value["schema_version"]
        non_blocking = value["non_blocking"]
        strings = (
            value["action"],
            value["dispatch_id"],
            value["claim_token"],
            value["bind_challenge"],
            value["model"],
            value["reasoning_effort"],
            value["workspace_path"],
            value["prompt"],
        )
        if isinstance(version, bool) or not isinstance(version, int) or non_blocking is not True:
            raise AppNativeError("App-native action scalar values are invalid")
        if any(not isinstance(item, str) for item in strings) or not isinstance(value["output_schema"], Mapping):
            raise AppNativeError("App-native action fields have invalid types")
        return cls(
            version,
            DispatchId(strings[1]),
            strings[2],
            strings[3],
            strings[4],
            ReasoningEffort(strings[5]),
            Path(strings[6]),
            strings[7],
            dict(value["output_schema"]),
            action=strings[0],
            non_blocking=non_blocking,
        )

    @property
    def sha256(self) -> str:
        payload = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HostReceipt:
    """Closed host assertion for the one native task result it observed.

    The HMAC proves possession of the controller-issued action capability.  It
    does not attest the Codex App itself: the hosting workflow is the trusted
    authority that must populate host/thread only from the native tool result.
    """

    schema_version: int
    dispatch_id: DispatchId
    action_sha256: str
    bind_challenge: str
    identity: HostIdentity
    proof: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AppNativeError("unsupported host receipt schema version")
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        for value, label in (
            (self.action_sha256, "action digest"),
            (self.bind_challenge, "bind challenge"),
            (self.proof, "receipt proof"),
        ):
            if _TOKEN_PATTERN.fullmatch(value) is None:
                raise AppNativeError(f"host receipt {label} must be lowercase 256-bit hexadecimal")
        if not isinstance(self.identity, HostIdentity):
            raise AppNativeError("host receipt identity must be typed")

    def _message(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "dispatch_id": str(self.dispatch_id),
            "action_sha256": self.action_sha256,
            "bind_challenge": self.bind_challenge,
            **self.identity.to_json(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    @classmethod
    def from_native_result(cls, action: AppNativeTaskAction, identity: HostIdentity) -> HostReceipt:
        provisional = cls(1, action.dispatch_id, action.sha256, action.bind_challenge, identity, "0" * 64)
        proof = hmac.new(action.claim_token.encode("ascii"), provisional._message(), hashlib.sha256).hexdigest()
        return cls(1, action.dispatch_id, action.sha256, action.bind_challenge, identity, proof)

    def verify(self, action: AppNativeTaskAction, *, claim_token: str) -> None:
        if (
            self.dispatch_id != action.dispatch_id
            or self.action_sha256 != action.sha256
            or self.bind_challenge != action.bind_challenge
        ):
            raise AppNativeError("host receipt does not match the prepared action")
        expected = hmac.new(claim_token.encode("ascii"), self._message(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(self.proof, expected):
            raise AppNativeError("host receipt proof is invalid")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "dispatch_id": str(self.dispatch_id),
            "action_sha256": self.action_sha256,
            "bind_challenge": self.bind_challenge,
            **self.identity.to_json(),
            "proof": self.proof,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> HostReceipt:
        expected = {"schema_version", "dispatch_id", "action_sha256", "bind_challenge", "host_id", "thread_id", "proof"}
        if set(value) != expected:
            raise AppNativeError(f"host receipt keys must be exactly {sorted(expected)!r}")
        if isinstance(value["schema_version"], bool) or not isinstance(value["schema_version"], int):
            raise AppNativeError("host receipt schema version must be an integer")
        strings = tuple(value[key] for key in expected - {"schema_version"})
        if any(not isinstance(item, str) for item in strings):
            raise AppNativeError("host receipt fields must be strings")
        return cls(
            value["schema_version"],
            DispatchId(value["dispatch_id"]),
            value["action_sha256"],
            value["bind_challenge"],
            HostIdentity(value["host_id"], ThreadIdentity(value["thread_id"])),
            value["proof"],
        )


@dataclass(frozen=True, slots=True)
class AppNativeDispatchRecord:
    action: AppNativeTaskAction
    state: AppNativeState
    identity: HostIdentity | None
    result: ModelFacingResult | None
    workspace_baseline_head_sha: str
    workspace_baseline: tuple[tuple[str, str], ...]
    git_authority_before_sha256: str
    git_authority_after_sha256: str | None
    controller_state_sha256: str
    workspace_terminal_head_sha: str | None
    workspace_terminal: tuple[tuple[str, str], ...] | None
    prepared_at: str
    bound_at: str | None
    completed_at: str | None

    @property
    def recovery(self) -> str:
        if self.state is AppNativeState.PREPARED:
            return "bind_the_exact_identity_returned_by_the_already_issued_native_action"
        if self.state is AppNativeState.BOUND:
            return "await_or_ingest_one_terminal_worker_result"
        return "terminal"

    def to_json(self) -> JsonObject:
        return {
            "action": self.action.to_json(),
            "state": self.state.value,
            "identity": self.identity.to_json() if self.identity is not None else None,
            "result": self.result.to_json() if self.result is not None else None,
            "workspace_baseline_head_sha": self.workspace_baseline_head_sha,
            "git_authority_before_sha256": self.git_authority_before_sha256,
            "git_authority_after_sha256": self.git_authority_after_sha256,
            "controller_state_sha256": self.controller_state_sha256,
            "workspace_terminal_head_sha": self.workspace_terminal_head_sha,
            "prepared_at": self.prepared_at,
            "bound_at": self.bound_at,
            "completed_at": self.completed_at,
            "recovery": self.recovery,
        }


__all__ = [
    "AppNativeDispatchRecord",
    "AppNativeError",
    "AppNativeState",
    "AppNativeTaskAction",
    "HostIdentity",
    "HostReceipt",
    "HostingMode",
    "claim_token_sha256",
]
