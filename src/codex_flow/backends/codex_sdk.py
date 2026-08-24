"""The only H1 production Codex transport: the published Python SDK.

This adapter intentionally imports and touches ``openai_codex`` only here.
Tests inject a small SDK-shaped fake, while production uses the package's
high-level ``Codex``/``Thread``/``TurnHandle`` surface.  There is no CLI or
direct app-server fallback.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

from ..domain import (
    Capability,
    CapabilityObservation,
    CapabilityStatus,
    CodexFlowError,
    JsonObject,
    LifecycleEvent,
    ReasoningEffort,
    Sandbox,
    Schema,
    SkillInput,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    UnsupportedCapability,
    validate_output_schema,
)


class _SdkThread(Protocol):
    id: str

    def turn(self, input: object, **kwargs: Any) -> _SdkTurn: ...


class _SdkTurn(Protocol):
    id: str

    def stream(self) -> Iterable[Any]: ...


class _SdkNotification(Protocol):
    method: str
    payload: object


class _SdkClient(Protocol):
    metadata: Any

    def thread_start(self, **kwargs: Any) -> _SdkThread: ...

    def thread_resume(self, thread_id: str, **kwargs: Any) -> _SdkThread: ...

    def thread_archive(self, thread_id: str) -> object: ...

    def close(self) -> None: ...


class _SdkSurface(Protocol):
    Codex: Callable[[], _SdkClient]
    Sandbox: Any
    ApprovalMode: Any
    SkillInput: Callable[..., object] | None
    ReasoningEffort: Any
    version: str


@dataclass(frozen=True, slots=True)
class CodexSdkConfig:
    """Explicit configuration for one SDK connection and thread."""

    model: str
    reasoning_effort: ReasoningEffort
    sandbox: Sandbox = Sandbox.READ_ONLY
    cwd: Path | None = None
    ephemeral: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must be explicit and non-empty")
        if self.sandbox not in {Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE}:
            raise ValueError("controller SDK execution supports read-only or workspace-write sandboxing")
        if self.cwd is not None and not self.cwd.is_dir():
            raise ValueError(f"SDK working directory does not exist: {self.cwd}")


def _load_sdk() -> _SdkSurface:
    """Load only the published SDK symbols used by this adapter."""

    try:
        package = importlib.import_module("openai_codex")
        types = importlib.import_module("openai_codex.types")
    except ImportError as exc:  # pragma: no cover - exercised by installation gate
        raise CodexFlowError("openai-codex is not installed; run `uv sync` before using the SDK adapter") from exc
    return cast(
        _SdkSurface,
        SimpleNamespace(
            Codex=package.Codex,
            Sandbox=package.Sandbox,
            ApprovalMode=package.ApprovalMode,
            SkillInput=package.SkillInput,
            ReasoningEffort=types.ReasoningEffort,
            version=package.__version__,
        ),
    )


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    try:
        return vars(value).get(key, default)
    except TypeError:
        return default


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return value.get("value", value)
    try:
        return vars(value).get("value", value)
    except TypeError:
        return value


def _identity(raw_thread: _SdkThread) -> ThreadIdentity:
    return ThreadIdentity(raw_thread.id)


def _turn_id(raw_turn: _SdkTurn) -> str:
    if not raw_turn.id:
        raise ValueError("SDK turn handle did not contain a string id")
    return raw_turn.id


def _wire_sandbox(sdk: _SdkSurface, sandbox: Sandbox) -> Any:
    try:
        if sandbox is Sandbox.READ_ONLY:
            return sdk.Sandbox.read_only
        if sandbox is Sandbox.WORKSPACE_WRITE:
            return sdk.Sandbox.workspace_write
        return sdk.Sandbox.full_access
    except AttributeError as exc:
        raise UnsupportedCapability(f"installed SDK does not expose sandbox preset {sandbox.value}") from exc


def _wire_effort(sdk: _SdkSurface, effort: ReasoningEffort) -> Any:
    try:
        if effort is ReasoningEffort.NONE:
            return sdk.ReasoningEffort.none
        if effort is ReasoningEffort.MINIMAL:
            return sdk.ReasoningEffort.minimal
        if effort is ReasoningEffort.LOW:
            return sdk.ReasoningEffort.low
        if effort is ReasoningEffort.MEDIUM:
            return sdk.ReasoningEffort.medium
        if effort is ReasoningEffort.HIGH:
            return sdk.ReasoningEffort.high
        return sdk.ReasoningEffort.xhigh
    except AttributeError as exc:
        raise UnsupportedCapability(f"installed SDK does not expose reasoning effort {effort.value}") from exc


def _wire_approval_mode(sdk: _SdkSurface) -> Any:
    try:
        return sdk.ApprovalMode.deny_all
    except AttributeError as exc:
        raise UnsupportedCapability("installed SDK does not expose deny-all approvals") from exc


def _wire_input(sdk: _SdkSurface, value: str | SkillInput) -> Any:
    if isinstance(value, str):
        return value
    skill_constructor = sdk.SkillInput
    if not callable(skill_constructor):
        raise UnsupportedCapability("structured skill input is not exposed by the SDK")
    return skill_constructor(name=value.name, path=value.path)


def _event_from_notification(event: _SdkNotification, sequence: int) -> LifecycleEvent:
    if not event.method:
        raise TerminalFailureAfterIdentity("SDK emitted a lifecycle event without a method")
    payload = event.payload
    turn_id = _read(payload, "turn_id") or _read(payload, "turnId")
    if turn_id is None:
        turn = _read(payload, "turn")
        turn_id = _read(turn, "id")
    return LifecycleEvent(sequence=sequence, method=event.method, turn_id=turn_id)


def _status_from_notification(event: Any) -> tuple[str | None, str | None]:
    payload = cast(_SdkNotification, event).payload
    turn = _read(payload, "turn")
    status = _enum_value(_read(turn, "status"))
    error = _read(turn, "error")
    message = _read(error, "message")
    return (status if isinstance(status, str) else None, message if isinstance(message, str) else None)


def _response_from_notification(event: Any) -> str | None:
    payload = cast(_SdkNotification, event).payload
    item = _read(payload, "item")
    root = _read(item, "root", item)
    text = _read(root, "text")
    return text if isinstance(text, str) else None


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate_decoded_schema(value: Any, schema: Mapping[str, Any], *, path: str = "output") -> None:
    expected = schema["type"]
    if not isinstance(expected, str) or not _type_matches(value, expected):
        raise TerminalFailureAfterIdentity(f"schema-bounded turn field {path!r} has the wrong JSON type")
    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise TerminalFailureAfterIdentity("schema-bounded turn used a malformed object schema")
        missing = [name for name in required if name not in value]
        if missing:
            raise TerminalFailureAfterIdentity(f"schema-bounded turn omitted required fields: {', '.join(missing)}")
        for key, property_schema in properties.items():
            if key in value:
                if not isinstance(property_schema, Mapping):
                    raise TerminalFailureAfterIdentity("schema-bounded turn used a malformed property schema")
                _validate_decoded_schema(value[key], property_schema, path=f"{path}.{key}")
        if schema.get("additionalProperties", True) is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise TerminalFailureAfterIdentity(
                    f"schema-bounded turn emitted unexpected fields: {', '.join(unexpected)}"
                )
    elif expected == "array" and "items" in schema:
        items = schema["items"]
        if not isinstance(items, Mapping):
            raise TerminalFailureAfterIdentity("schema-bounded turn used a malformed array schema")
        for index, item in enumerate(value):
            _validate_decoded_schema(item, items, path=f"{path}[{index}]")


def _decode_schema_output(response: str | None, schema: Schema | None) -> JsonObject | None:
    if schema is None:
        return None
    try:
        validate_output_schema(schema)
    except ValueError as exc:
        raise TerminalFailureAfterIdentity("schema-bounded turn used a malformed output schema") from exc
    if not isinstance(response, str):
        raise TerminalFailureAfterIdentity("schema-bounded turn did not return a text response")
    try:
        decoded = json.loads(response)
    except json.JSONDecodeError as exc:
        raise TerminalFailureAfterIdentity("schema-bounded turn returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise TerminalFailureAfterIdentity("schema-bounded turn returned a non-object JSON value")
    _validate_decoded_schema(decoded, schema)
    return cast(JsonObject, decoded)


class CodexSdkAdapter:
    """Typed adapter around the stable high-level ``openai-codex`` API."""

    def __init__(
        self,
        config: CodexSdkConfig,
        *,
        client_factory: Callable[[], _SdkClient] | None = None,
        sdk: object | None = None,
    ) -> None:
        self.config = config
        self._sdk = cast(_SdkSurface, sdk or _load_sdk())
        self._client_factory = client_factory or (lambda: self._sdk.Codex())
        self._client: _SdkClient | None = None
        self._threads: dict[str, _SdkThread] = {}

    @property
    def sdk_version(self) -> str:
        return self._sdk.version

    @property
    def runtime_version(self) -> str:
        """Return the app-server version reported by SDK initialization."""

        client = self._client
        if client is None:
            return "unknown"
        try:
            version = client.metadata.serverInfo.version
        except AttributeError:
            return "unknown"
        return version if isinstance(version, str) and version else "unknown"

    def __enter__(self) -> CodexSdkAdapter:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def close(self) -> None:
        client = self._client
        self._client = None
        self._threads.clear()
        if client is not None:
            client.close()

    def _require_client(self) -> _SdkClient:
        if self._client is None:
            raise CodexFlowError("SDK adapter is not connected; call start_thread first")
        return self._client

    def start_thread(self) -> ThreadIdentity:
        """Start a local thread, classifying any pre-identity failure as transport."""

        if self._client is not None:
            raise CodexFlowError("SDK adapter already owns a thread connection")
        try:
            client = self._client_factory()
            self._client = client
            raw_thread = client.thread_start(
                approval_mode=_wire_approval_mode(self._sdk),
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                model=self.config.model,
                sandbox=_wire_sandbox(self._sdk, self.config.sandbox),
                ephemeral=self.config.ephemeral,
            )
            identity = _identity(raw_thread)
        except UnsupportedCapability:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise TransportFailureBeforeIdentity("SDK start failed before a thread identity was returned") from exc
        self._threads[identity.id] = raw_thread
        return identity

    def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity:
        """Resume exactly the addressable thread and reject identity changes."""

        created_client = self._client is None
        try:
            if self._client is None:
                self._client = self._client_factory()
            client = self._require_client()
            raw_thread = client.thread_resume(
                thread.id,
                approval_mode=_wire_approval_mode(self._sdk),
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                model=self.config.model,
                sandbox=_wire_sandbox(self._sdk, self.config.sandbox),
            )
            resumed = _identity(raw_thread)
        except UnsupportedCapability:
            if created_client:
                self.close()
            raise
        except Exception as exc:
            if created_client:
                self.close()
            raise TerminalFailureAfterIdentity(f"SDK resume failed after thread identity {thread.id!r}") from exc
        if resumed.id != thread.id:
            if created_client:
                self.close()
            raise TerminalFailureAfterIdentity(
                f"SDK resume changed thread identity from {thread.id!r} to {resumed.id!r}"
            )
        self._threads[thread.id] = raw_thread
        return resumed

    def archive_thread(self, thread: ThreadIdentity) -> None:
        """Archive a thread created by a compatibility sentinel after proof."""

        client = self._require_client()
        try:
            client.thread_archive(thread.id)
        except Exception as exc:
            raise TerminalFailureAfterIdentity(f"SDK archive failed after thread identity {thread.id!r}") from exc

    def run_turn(
        self,
        thread: ThreadIdentity,
        input: str | SkillInput,
        *,
        output_schema: Schema | None = None,
    ) -> TurnObservation:
        """Run one turn, retain ordered lifecycle events, and normalize the result."""

        raw_thread = self._threads.get(thread.id)
        if raw_thread is None:
            raise CodexFlowError(f"unknown SDK thread identity: {thread.id}")
        try:
            raw_turn = raw_thread.turn(
                _wire_input(self._sdk, input),
                approval_mode=_wire_approval_mode(self._sdk),
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                effort=_wire_effort(self._sdk, self.config.reasoning_effort),
                model=self.config.model,
                output_schema=dict(output_schema) if output_schema is not None else None,
                sandbox=_wire_sandbox(self._sdk, self.config.sandbox),
            )
            turn_id = _turn_id(raw_turn)
            events: list[LifecycleEvent] = []
            final_response: str | None = None
            status: str | None = None
            error: str | None = None
            for sequence, raw_event in enumerate(raw_turn.stream()):
                event = _event_from_notification(raw_event, sequence)
                events.append(event)
                if event.method == "item/completed":
                    candidate = _response_from_notification(raw_event)
                    if candidate is not None:
                        final_response = candidate
                if event.method == "turn/completed":
                    status, error = _status_from_notification(raw_event)
            if status is None:
                raise TerminalFailureAfterIdentity(f"SDK turn {turn_id!r} ended without a turn/completed event")
            if status != "completed":
                raise TerminalFailureAfterIdentity(error or f"SDK turn {turn_id!r} ended with status {status!r}")
            structured = _decode_schema_output(final_response, output_schema)
            return TurnObservation(
                thread_id=thread,
                turn_id=turn_id,
                status=status,
                final_response=final_response,
                structured_output=structured,
                events=tuple(events),
                error=error,
            )
        except TerminalFailureAfterIdentity:
            raise
        except UnsupportedCapability:
            raise
        except Exception as exc:
            raise TerminalFailureAfterIdentity(f"SDK turn failed after thread identity {thread.id!r}") from exc

    def validate_skill_input(self, value: SkillInput) -> SkillInput:
        """Return typed skill input only when the installed SDK advertises it."""

        if self._sdk.SkillInput is None:
            raise UnsupportedCapability("structured skill input is not exposed by the SDK")
        return value

    def capability_matrix(self) -> tuple[CapabilityObservation, ...]:
        """Return availability labels without claiming that an operation ran."""

        required = (
            Capability.LOCAL_START,
            Capability.THREAD_IDENTITY,
            Capability.SCHEMA_BOUNDED_TURN,
            Capability.EXPLICIT_MODEL,
            Capability.EXPLICIT_REASONING_EFFORT,
            Capability.LIFECYCLE_EVENTS,
            Capability.SAME_THREAD_RESUME,
            Capability.SANDBOX_ISOLATION,
        )
        observations = [
            CapabilityObservation(capability, CapabilityStatus.NOT_RUN, "real sentinel not run")
            for capability in required
        ]
        skill_input_available = self._sdk.SkillInput is not None
        observations.extend(
            [
                CapabilityObservation(
                    Capability.REVIEW,
                    CapabilityStatus.NOT_EXPOSED,
                    "stable Codex/Thread API has no high-level review operation",
                ),
                CapabilityObservation(
                    Capability.STRUCTURED_SKILL_INPUT,
                    CapabilityStatus.NOT_RUN if skill_input_available else CapabilityStatus.UNSUPPORTED,
                    "public SkillInput type is available but not exercised by the read-only sentinel"
                    if skill_input_available
                    else "SDK has no public SkillInput type",
                ),
                CapabilityObservation(
                    Capability.DESKTOP,
                    CapabilityStatus.NOT_EXPOSED,
                    "Desktop UI control is outside the SDK transport surface",
                ),
                CapabilityObservation(
                    Capability.IDLE_WAKE,
                    CapabilityStatus.NOT_EXPOSED,
                    "idle wake/queue control is outside the SDK transport surface",
                ),
                CapabilityObservation(
                    Capability.REMOTE_HOST,
                    CapabilityStatus.NOT_EXPOSED,
                    "remote host selection is outside the local SDK transport surface",
                ),
                CapabilityObservation(
                    Capability.PERMISSION_PROFILE,
                    CapabilityStatus.NOT_EXPOSED,
                    "permission-profile survival is not exposed by the high-level SDK",
                ),
            ]
        )
        return tuple(observations)


__all__ = ["CodexSdkAdapter", "CodexSdkConfig"]
