"""Controller-owned types at the stable SDK boundary.

Raw ``openai_codex`` objects deliberately do not cross this module boundary.
The controller can therefore test transport behaviour without starting a
Codex process or importing the SDK in its domain code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
Schema: TypeAlias = Mapping[str, JsonValue]
MAX_JSON_BYTES = 16 * 1_048_576

# Live-worker diagnostics are deliberately small and non-authoritative.  The
# ring stores redacted snippets and digests only; terminal lifecycle facts stay
# in the queue/result tables.
DIAGNOSTIC_RING_MAX_ENTRIES = 128
DIAGNOSTIC_RING_MAX_BYTES = 64 * 1024
DIAGNOSTIC_TEXT_MAX_BYTES = 8 * 1024
# Only lifecycle boundaries have durable diagnostic value.  Progress deltas
# are intentionally acknowledged at the IPC boundary without entering the
# SQLite ring: the worker heartbeat remains the lease authority and terminal
# result ingress remains the lifecycle authority.
WORKER_DIAGNOSTIC_RETAINED_METHODS = frozenset(
    {
        "turn/started",
        "turn/completed",
        "turn/failed",
        "turn/interrupted",
        "item/completed",
    }
)
STEER_TEXT_MAX_BYTES = 8 * 1024
CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES = frozenset(
    {"completed", "needs_decision", "external_blocked", "failed", "interrupted"}
)


class StrictJSONError(ValueError):
    """A JSON payload is not an unambiguous RFC-style JSON value."""


class _FrozenList(tuple[object, ...]):
    """Immutable JSON array retaining ordinary list equality semantics."""

    __hash__ = tuple.__hash__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list | tuple):
            return tuple(self) == tuple(other)
        return NotImplemented


def _strict_json_constant(value: str) -> object:
    # ``parse_constant`` is called for all three non-standard tokens.  Do not
    # include the provider token in the exception: provider content must never
    # become durable error text.
    raise StrictJSONError("JSON contains a non-standard numeric constant")


def _strict_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StrictJSONError("JSON contains a non-finite number")
    return result


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _validate_interoperable_json(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise StrictJSONError("JSON contains an unpaired Unicode surrogate")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_interoperable_json(key)
            _validate_interoperable_json(child)
        return
    if isinstance(value, list | tuple):
        for child in value:
            _validate_interoperable_json(child)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictJSONError("JSON contains a non-finite number")


def strict_json_loads(value: str | bytes | bytearray, *, max_bytes: int = MAX_JSON_BYTES) -> object:
    """Decode only unambiguous, finite JSON values.

    The decoder rejects NaN/Infinity, oversized exponents that become
    non-finite, duplicate keys recursively at every object depth, non-UTF-8
    byte encodings, and every Unicode byte-order mark.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("JSON byte limit must be a positive integer")
    if isinstance(value, bytes | bytearray):
        raw = bytes(value)
        if len(raw) > max_bytes:
            raise StrictJSONError("JSON exceeds the byte limit")
        if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            raise StrictJSONError("JSON must be strict UTF-8 without a byte-order mark")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("JSON is not valid strict UTF-8") from exc
    elif isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise StrictJSONError("JSON contains invalid Unicode") from exc
        if len(encoded) > max_bytes:
            raise StrictJSONError("JSON exceeds the byte limit")
        text = value
    else:
        raise StrictJSONError("JSON input must be text or bytes")
    if text.startswith("\ufeff"):
        raise StrictJSONError("JSON must be strict UTF-8 without a byte-order mark")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_strict_json_constant,
            parse_float=_strict_json_float,
        )
        _validate_interoperable_json(decoded)
        return decoded
    except StrictJSONError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise StrictJSONError("JSON is not valid interoperable data") from exc


def freeze_json(value: object) -> object:
    """Detach JSON-compatible mappings/lists into immutable owned values."""

    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        try:
            entries = tuple(value.items())
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("JSON mappings must expose one stable item snapshot") from exc
        for entry in entries:
            if isinstance(entry, str | bytes | bytearray):
                raise ValueError("JSON mappings must expose key/value pairs")
            try:
                pair = tuple(entry)
            except (TypeError, ValueError, RuntimeError) as exc:
                raise ValueError("JSON mappings must expose key/value pairs") from exc
            if len(pair) != 2:
                raise ValueError("JSON mappings must expose key/value pairs")
            key, child = pair
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            if key in frozen:
                raise ValueError("JSON mappings must not contain duplicate keys")
            frozen[key] = freeze_json(child)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        try:
            entries = tuple(value)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("JSON sequences must expose one stable item snapshot") from exc
        return _FrozenList(freeze_json(child) for child in entries)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON values must contain only finite numbers")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON strings must not contain unpaired Unicode surrogates")
        return value
    if value is None or isinstance(value, int | bool | float):
        return value
    raise ValueError("JSON values must use supported scalar, mapping, and list types")


def thaw_json(value: object) -> object:
    """Return a detached mutable JSON tree suitable for SDK/JSON APIs."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(child) for child in value]
    return value


class Sandbox(str, Enum):
    """Filesystem policy accepted by the SDK adapter."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class NativePermissionMode(str, Enum):
    INHERIT_NATIVE = "inherit_native"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class NativePermissionAuthority:
    """Effective native authority retained monotonically across SDK processes."""

    mode: NativePermissionMode
    sandbox_mode: str
    approval_policy: str

    def __post_init__(self) -> None:
        if self.sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("effective native sandbox mode is unsupported")
        if self.approval_policy not in {"untrusted", "on-request", "never"}:
            raise ValueError("effective native approval policy is unsupported")
        if self.mode is NativePermissionMode.READ_ONLY and self.sandbox_mode != "read-only":
            raise ValueError("read-only capsule mode must retain a read-only sandbox")

    @property
    def facts(self) -> JsonObject:
        return {
            "mode": self.mode.value,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "monotonic": True,
        }

    @classmethod
    def from_facts(cls, value: Mapping[str, JsonValue]) -> NativePermissionAuthority:
        if set(value) != {"mode", "sandbox_mode", "approval_policy", "monotonic"}:
            raise ValueError("effective native permission facts have an unsupported shape")
        if value.get("monotonic") is not True:
            raise ValueError("effective native permission facts are not monotonic")
        mode = value.get("mode")
        sandbox = value.get("sandbox_mode")
        approval = value.get("approval_policy")
        if not isinstance(mode, str) or not isinstance(sandbox, str) or not isinstance(approval, str):
            raise ValueError("effective native permission facts have invalid values")
        return cls(NativePermissionMode(mode), sandbox, approval)

    def meet(self, candidate: NativePermissionAuthority) -> NativePermissionAuthority:
        """Return the no-broader authority, rejecting incomparable approvals."""

        sandbox_rank = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
        sandbox = min((self.sandbox_mode, candidate.sandbox_mode), key=sandbox_rank.__getitem__)
        if self.approval_policy == candidate.approval_policy:
            approval = self.approval_policy
        elif "never" in {self.approval_policy, candidate.approval_policy}:
            approval = "never"
        else:
            raise ValueError("native approval policies are incomparable")
        mode = (
            NativePermissionMode.READ_ONLY
            if NativePermissionMode.READ_ONLY in {self.mode, candidate.mode}
            else NativePermissionMode.INHERIT_NATIVE
        )
        return NativePermissionAuthority(mode, sandbox, approval)


class ReasoningEffort(str, Enum):
    """Explicit reasoning effort; no implicit SDK default is accepted."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class Capability(str, Enum):
    """Capabilities recorded by the H1 compatibility sentinel."""

    LOCAL_START = "local_start"
    THREAD_IDENTITY = "thread_identity"
    SCHEMA_BOUNDED_TURN = "schema_bounded_turn"
    EXPLICIT_MODEL = "explicit_model"
    EXPLICIT_REASONING_EFFORT = "explicit_reasoning_effort"
    LIFECYCLE_EVENTS = "lifecycle_events"
    SAME_THREAD_RESUME = "same_thread_resume"
    SANDBOX_ISOLATION = "sandbox_isolation"
    REVIEW = "review"
    STRUCTURED_SKILL_INPUT = "structured_skill_input"
    DESKTOP = "desktop"
    IDLE_WAKE = "idle_wake"
    REMOTE_HOST = "remote_host"
    PERMISSION_PROFILE = "permission_profile"


class CapabilityStatus(str, Enum):
    """Truthful compatibility labels; status is never inferred from presence."""

    PROVEN = "proven"
    UNSUPPORTED = "unsupported"
    NOT_EXPOSED = "not_exposed"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    capability: Capability
    status: CapabilityStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ThreadIdentity:
    """Addressable SDK thread identity owned by the controller."""

    id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or "\x00" in self.id
            or any(character.isspace() for character in self.id)
            or len(self.id.encode("utf-8")) > 512
        ):
            raise ValueError("thread identity must be a bounded non-empty, whitespace-free string")


@dataclass(frozen=True, slots=True)
class SkillInput:
    """Structured skill reference accepted by the SDK input surface."""

    name: str
    path: str

    def __post_init__(self) -> None:
        if not self.name or not self.path:
            raise ValueError("skill input requires a name and path")


@dataclass(frozen=True, slots=True)
class LocalImageInput:
    """One validated repository-local image path for an SDK turn."""

    path: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path
            or len(os.fsencode(self.path)) > 4_096
            or Path(self.path).is_absolute()
            or self.path == "."
            or ".." in Path(self.path).parts
            or Path(self.path).as_posix() != self.path
            or "\x00" in self.path
        ):
            raise ValueError("local image input path must be repository-relative")


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    sequence: int
    method: str
    turn_id: str | None = None
    text: str | None = None
    occurred_at: str | None = None


@dataclass(frozen=True, slots=True)
class TurnObservation:
    thread_id: ThreadIdentity
    turn_id: str
    status: str
    final_response: str | None
    structured_output: JsonObject | None
    events: tuple[LifecycleEvent, ...]
    error: str | None = None


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_diagnostic_text(value: str, *, limit: int = DIAGNOSTIC_TEXT_MAX_BYTES) -> str:
    """Bound and redact provider text before it can enter durable evidence."""

    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("diagnostic text must be a string without NUL")
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", text)
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) > limit:
        encoded = encoded[:limit]
        text = encoded.decode("utf-8", errors="ignore")
    return text


def is_lossy_worker_diagnostic_method(method: str) -> bool:
    """Return whether one worker event is safe to acknowledge without storage."""

    return method not in WORKER_DIAGNOSTIC_RETAINED_METHODS


def redact_control_text(value: str, *, limit: int = STEER_TEXT_MAX_BYTES) -> str:
    """Redact bounded human control text before it crosses a durable boundary."""

    if isinstance(value, str) and "\x00" not in value:
        return redact_diagnostic_text(value, limit=limit)
    raise ValueError("control text must be a string without NUL")


# Conversation history is a read-only projection.  It deliberately has a
# separate redactor because the diagnostic helper truncates, while history
# must either return the complete redacted text or fail closed at its source
# limits.
CONVERSATION_MAX_TURNS = 4_096
CONVERSATION_MAX_ITEMS = 65_536
CONVERSATION_MAX_TEXT_BYTES = 16 * 1_048_576
CONVERSATION_FRAGMENT_BYTES = 8 * 1_024
CONVERSATION_PAGE_FRAGMENTS = 32
CONVERSATION_PAGE_BYTES = 48 * 1_024
CONVERSATION_MAX_CONCURRENT_READS = 4
CONVERSATION_READ_DEADLINE_SECONDS = 5.0


class ConversationSubjectKind(str, Enum):
    WORKER = "worker"
    CONTROLLER = "controller"


class ConversationSpeaker(str, Enum):
    USER = "user"
    AGENT = "agent"


class ConversationContentKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    SKILL = "skill"
    MENTION = "mention"


class ConversationHistoryStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DELETED = "deleted"
    PERMISSION_DENIED = "permission_denied"
    SOURCE_INCOMPLETE = "source_incomplete"
    SOURCE_TOO_LARGE = "source_too_large"
    STALE = "stale"
    MALFORMED = "malformed"


def redact_conversation_text(value: str) -> tuple[str, bool]:
    """Redact known secrets without truncating conversation content."""

    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("conversation text must be a string without NUL")
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", text)
    return text, text != value


@dataclass(frozen=True, slots=True)
class ConversationHistoryRequest:
    """One non-durable, identity-bound conversation page request."""

    subject_kind: ConversationSubjectKind
    subject_id: str
    thread_id: ThreadIdentity
    generation: int | None = None
    attempt: int | None = None
    revision: int | None = None
    page_token: str | None = None
    page_fragments: int = CONVERSATION_PAGE_FRAGMENTS

    def __post_init__(self) -> None:
        if not isinstance(self.subject_kind, ConversationSubjectKind):
            object.__setattr__(self, "subject_kind", ConversationSubjectKind(self.subject_kind))
        if not isinstance(self.subject_id, str) or not self.subject_id or len(self.subject_id.encode("utf-8")) > 512:
            raise ValueError("conversation subject identity is invalid")
        if not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))
        for name, value in (("generation", self.generation), ("attempt", self.attempt), ("revision", self.revision)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"conversation {name} is invalid")
        if self.page_token is not None and (
            not isinstance(self.page_token, str) or len(self.page_token.encode("utf-8")) > 1024
        ):
            raise ValueError("conversation page token is invalid")
        if (
            isinstance(self.page_fragments, bool)
            or not isinstance(self.page_fragments, int)
            or not 1 <= self.page_fragments <= CONVERSATION_PAGE_FRAGMENTS
        ):
            raise ValueError("conversation page size is invalid")

    def to_json(self) -> JsonObject:
        return {
            "subject_kind": self.subject_kind.value,
            "subject_id": self.subject_id,
            "thread_id": self.thread_id.id,
            "generation": self.generation,
            "attempt": self.attempt,
            "revision": self.revision,
            "page_token": self.page_token,
            "page_fragments": self.page_fragments,
        }


@dataclass(frozen=True, slots=True)
class ConversationContent:
    kind: ConversationContentKind
    text: str | None = None
    redacted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConversationContentKind):
            object.__setattr__(self, "kind", ConversationContentKind(self.kind))
        if self.text is not None:
            if not isinstance(self.text, str) or "\x00" in self.text:
                raise ValueError("conversation content text is invalid")
            if len(self.text.encode("utf-8")) > CONVERSATION_FRAGMENT_BYTES:
                raise ValueError("conversation content fragment exceeds its byte limit")
        if not isinstance(self.redacted, bool):
            raise ValueError("conversation redaction flag is invalid")


@dataclass(frozen=True, slots=True)
class ConversationMessageFragment:
    turn_id: str
    item_id: str
    speaker: ConversationSpeaker
    content: ConversationContent
    ordinal: int

    def __post_init__(self) -> None:
        for name, value in (("turn id", self.turn_id), ("item id", self.item_id)):
            if (
                not isinstance(value, str)
                or not value
                or any(character.isspace() for character in value)
                or len(value.encode("utf-8")) > 512
            ):
                raise ValueError(f"conversation {name} is invalid")
        if not isinstance(self.speaker, ConversationSpeaker):
            object.__setattr__(self, "speaker", ConversationSpeaker(self.speaker))
        if not isinstance(self.content, ConversationContent):
            raise ValueError("conversation fragment content is not typed")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("conversation fragment ordinal is invalid")


@dataclass(frozen=True, slots=True)
class ConversationTurnSlice:
    turn_id: str
    ordinal: int
    messages: tuple[ConversationMessageFragment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, str) or not self.turn_id:
            raise ValueError("conversation turn identity is invalid")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("conversation turn ordinal is invalid")
        messages = tuple(self.messages)
        if not all(isinstance(message, ConversationMessageFragment) for message in messages):
            raise ValueError("conversation turn messages are not typed")
        object.__setattr__(self, "messages", messages)


@dataclass(frozen=True, slots=True)
class ConversationHistoryPage:
    subject_kind: ConversationSubjectKind
    subject_id: str
    thread_id: ThreadIdentity
    status: ConversationHistoryStatus
    snapshot_token: str | None
    turns: tuple[ConversationTurnSlice, ...] = ()
    older_token: str | None = None
    redaction_count: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.subject_kind, ConversationSubjectKind):
            object.__setattr__(self, "subject_kind", ConversationSubjectKind(self.subject_kind))
        if not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))
        if not isinstance(self.status, ConversationHistoryStatus):
            object.__setattr__(self, "status", ConversationHistoryStatus(self.status))
        if not isinstance(self.subject_id, str) or not self.subject_id or len(self.subject_id.encode("utf-8")) > 512:
            raise ValueError("conversation subject identity is invalid")
        for name, value in (("snapshot", self.snapshot_token), ("older page", self.older_token)):
            if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024):
                raise ValueError(f"conversation {name} token is invalid")
        turns = tuple(self.turns)
        if not all(isinstance(turn, ConversationTurnSlice) for turn in turns):
            raise ValueError("conversation page turns are not typed")
        if [turn.ordinal for turn in turns] != sorted({turn.ordinal for turn in turns}):
            raise ValueError("conversation turns are out of order or duplicated")
        if any(message.turn_id != turn.turn_id for turn in turns for message in turn.messages):
            raise ValueError("conversation message is bound to the wrong turn")
        message_ordinals = [message.ordinal for turn in turns for message in turn.messages]
        if message_ordinals != sorted(set(message_ordinals)):
            raise ValueError("conversation messages are out of order or duplicated")
        object.__setattr__(self, "turns", turns)
        if (
            isinstance(self.redaction_count, bool)
            or not isinstance(self.redaction_count, int)
            or self.redaction_count < 0
        ):
            raise ValueError("conversation redaction count is invalid")
        if not isinstance(self.reason, str) or len(self.reason.encode("utf-8")) > 512:
            raise ValueError("conversation history reason is invalid")
        if self.status is ConversationHistoryStatus.AVAILABLE:
            if self.snapshot_token is None:
                raise ValueError("available conversation page requires a snapshot token")
        elif self.snapshot_token is not None or self.older_token is not None or turns:
            raise ValueError("unavailable conversation page cannot carry transcript data")

    @property
    def complete(self) -> bool:
        return self.status is ConversationHistoryStatus.AVAILABLE and self.older_token is None

    def to_json(self) -> JsonObject:
        return {
            "subject_kind": self.subject_kind.value,
            "subject_id": self.subject_id,
            "thread_id": self.thread_id.id,
            "status": self.status.value,
            "snapshot_token": self.snapshot_token,
            "turns": [
                {
                    "turn_id": turn.turn_id,
                    "ordinal": turn.ordinal,
                    "messages": [
                        {
                            "turn_id": message.turn_id,
                            "item_id": message.item_id,
                            "speaker": message.speaker.value,
                            "ordinal": message.ordinal,
                            "content": {
                                "kind": message.content.kind.value,
                                "text": message.content.text,
                                "redacted": message.content.redacted,
                            },
                        }
                        for message in turn.messages
                    ],
                }
                for turn in self.turns
            ],
            "older_token": self.older_token,
            "redaction_count": self.redaction_count,
            "reason": self.reason,
        }


def conversation_history_page_from_json(value: object) -> ConversationHistoryPage:
    """Decode the one closed, non-persisted conversation page envelope."""

    if not isinstance(value, dict) or set(value) != {
        "subject_kind",
        "subject_id",
        "thread_id",
        "status",
        "snapshot_token",
        "turns",
        "older_token",
        "redaction_count",
        "reason",
    }:
        raise ValueError("conversation page shape is invalid")
    turns_raw = value["turns"]
    if not isinstance(turns_raw, list):
        raise ValueError("conversation turns are invalid")
    turns: list[ConversationTurnSlice] = []
    for turn_raw in turns_raw:
        if not isinstance(turn_raw, dict) or set(turn_raw) != {"turn_id", "ordinal", "messages"}:
            raise ValueError("conversation turn shape is invalid")
        messages_raw = turn_raw["messages"]
        if not isinstance(messages_raw, list):
            raise ValueError("conversation messages are invalid")
        messages: list[ConversationMessageFragment] = []
        for message_raw in messages_raw:
            if not isinstance(message_raw, dict) or set(message_raw) != {
                "turn_id",
                "item_id",
                "speaker",
                "ordinal",
                "content",
            }:
                raise ValueError("conversation message shape is invalid")
            content_raw = message_raw["content"]
            if not isinstance(content_raw, dict) or set(content_raw) != {"kind", "text", "redacted"}:
                raise ValueError("conversation content shape is invalid")
            messages.append(
                ConversationMessageFragment(
                    message_raw["turn_id"],  # type: ignore[arg-type]
                    message_raw["item_id"],  # type: ignore[arg-type]
                    ConversationSpeaker(message_raw["speaker"]),  # type: ignore[arg-type]
                    ConversationContent(
                        ConversationContentKind(content_raw["kind"]),  # type: ignore[arg-type]
                        content_raw["text"],  # type: ignore[arg-type]
                        content_raw["redacted"],  # type: ignore[arg-type]
                    ),
                    message_raw["ordinal"],  # type: ignore[arg-type]
                )
            )
        turns.append(
            ConversationTurnSlice(
                turn_raw["turn_id"],  # type: ignore[arg-type]
                turn_raw["ordinal"],  # type: ignore[arg-type]
                tuple(messages),
            )
        )
    return ConversationHistoryPage(
        ConversationSubjectKind(value["subject_kind"]),  # type: ignore[arg-type]
        value["subject_id"],  # type: ignore[arg-type]
        ThreadIdentity(value["thread_id"]),  # type: ignore[arg-type]
        ConversationHistoryStatus(value["status"]),  # type: ignore[arg-type]
        value["snapshot_token"],  # type: ignore[arg-type]
        tuple(turns),
        value["older_token"],  # type: ignore[arg-type]
        value["redaction_count"],  # type: ignore[arg-type]
        value["reason"],  # type: ignore[arg-type]
    )


class RetryFailureClass(str, Enum):
    """Closed failure classes used by durable live-worker retry policy."""

    PRE_IDENTITY_TRANSPORT = "pre_identity_transport"
    INVALID_RESPONSE_CHAIN = "invalid_response_chain"
    SCHEMA_ENVELOPE = "schema_envelope"
    POST_IDENTITY_LOSS = "post_identity_loss"
    PROVIDER_TRANSIENT = "provider_transient"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CAPABILITY = "capability"
    PROFILE = "profile"
    INTEGRITY = "integrity"
    MALFORMED_INPUT = "malformed_input"
    RESULT_TRANSPORT_AFTER_IDENTITY = "result_transport_after_identity"
    REPLAY = "replay"
    UNKNOWN = "unknown"


class WorkerResultRejectionCode(str, Enum):
    """Sanitized, typed reasons for rejecting a worker result at the IPC boundary."""

    CAPABILITY_EXPIRED = "result_capability_expired"
    CAPABILITY_STALE = "result_capability_stale"
    MALFORMED_OUTPUT = "malformed_model_output"


class RecoveryStrategy(str, Enum):
    NONE = "none"
    BACKOFF = "backoff"
    FRESH_THREAD = "fresh_thread"
    SAME_THREAD_SCHEMA_CORRECTION = "same_thread_schema_correction"
    SAME_THREAD_CONTINUATION = "same_thread_continuation"
    HUMAN_ATTENTION = "human_attention_required"


class ControlCommandKind(str, Enum):
    STEER = "steer"
    INTERRUPT = "interrupt"


class ControlCommandState(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """One bounded, redacted live-worker activity record."""

    sequence: int
    kind: str
    occurred_at: str
    text: str | None = None
    payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("diagnostic sequence must be a positive integer")
        _required_text(self.kind, label="diagnostic kind", limit=128)
        _required_text(self.occurred_at, label="diagnostic timestamp", limit=64)
        if self.text is not None:
            redacted = redact_diagnostic_text(self.text)
            if redacted != self.text:
                object.__setattr__(self, "text", redacted)
            if len(redacted.encode("utf-8")) > DIAGNOSTIC_TEXT_MAX_BYTES:
                raise ValueError("diagnostic text exceeds its byte limit")
        if self.payload_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256) is None:
            raise ValueError("diagnostic payload digest must be a lowercase SHA-256")

    @property
    def encoded_bytes(self) -> int:
        return len(
            json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    def to_json(self) -> JsonObject:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "text": self.text,
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class RetryPolicyFacts:
    """Immutable retry budgets and the mutable counters they govern."""

    revision: int = 0
    policy_version: int = 1
    pre_identity_budget: int = 5
    invalid_chain_budget: int = 1
    schema_envelope_budget: int = 2
    post_identity_loss_budget: int = 1
    provider_transient_budget: int = 3
    pre_identity_used: int = 0
    invalid_chain_used: int = 0
    schema_envelope_used: int = 0
    post_identity_loss_used: int = 0
    provider_transient_used: int = 0
    provider_transient_grant_used: int = 0
    last_failure: RetryFailureClass | None = None
    strategy: RecoveryStrategy = RecoveryStrategy.NONE
    next_eligible_at: str | None = None
    prior_thread_id: str | None = None
    prior_turn_id: str | None = None
    human_attention_reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("retry policy revision must be a non-negative integer")
        if self.policy_version != 1 or isinstance(self.policy_version, bool):
            raise ValueError("unsupported retry policy version")
        for name in (
            "pre_identity_budget",
            "invalid_chain_budget",
            "schema_envelope_budget",
            "post_identity_loss_budget",
            "provider_transient_budget",
            "pre_identity_used",
            "invalid_chain_used",
            "schema_envelope_used",
            "post_identity_loss_used",
            "provider_transient_used",
            "provider_transient_grant_used",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"retry policy {name} must be a non-negative integer")
        if self.pre_identity_used > self.pre_identity_budget:
            raise ValueError("pre-identity retry budget is consumed beyond its limit")
        if self.invalid_chain_used > self.invalid_chain_budget:
            raise ValueError("invalid-chain retry budget is consumed beyond its limit")
        if self.schema_envelope_used > self.schema_envelope_budget:
            raise ValueError("schema-envelope retry budget is consumed beyond its limit")
        if self.post_identity_loss_used > self.post_identity_loss_budget:
            raise ValueError("post-identity retry budget is consumed beyond its limit")
        if self.provider_transient_budget > 4:
            raise ValueError("provider transient retry budget exceeds its absolute ceiling")
        if self.provider_transient_used > self.provider_transient_budget:
            raise ValueError("provider transient retry budget is consumed beyond its limit")
        if self.provider_transient_grant_used not in {0, 1}:
            raise ValueError("provider transient grant state is invalid")
        if self.provider_transient_budget > 3 and self.provider_transient_grant_used != 1:
            raise ValueError("provider transient budget above the default requires its grant fact")
        if self.last_failure is not None and not isinstance(self.last_failure, RetryFailureClass):
            object.__setattr__(self, "last_failure", RetryFailureClass(self.last_failure))
        if not isinstance(self.strategy, RecoveryStrategy):
            object.__setattr__(self, "strategy", RecoveryStrategy(self.strategy))
        for name in ("next_eligible_at", "prior_thread_id", "prior_turn_id", "human_attention_reason"):
            value = getattr(self, name)
            if value is not None:
                _required_text(value, label=f"retry policy {name}", limit=512)

    def budget_for(self, failure: RetryFailureClass | str) -> tuple[int, int]:
        target = failure if isinstance(failure, RetryFailureClass) else RetryFailureClass(failure)
        mapping = {
            RetryFailureClass.PRE_IDENTITY_TRANSPORT: (self.pre_identity_budget, self.pre_identity_used),
            RetryFailureClass.INVALID_RESPONSE_CHAIN: (self.invalid_chain_budget, self.invalid_chain_used),
            RetryFailureClass.SCHEMA_ENVELOPE: (self.schema_envelope_budget, self.schema_envelope_used),
            RetryFailureClass.POST_IDENTITY_LOSS: (self.post_identity_loss_budget, self.post_identity_loss_used),
            RetryFailureClass.PROVIDER_TRANSIENT: (self.provider_transient_budget, self.provider_transient_used),
        }
        return mapping.get(target, (0, 0))

    def to_json(self) -> JsonObject:
        return {
            "revision": self.revision,
            "policy_version": self.policy_version,
            "pre_identity_budget": self.pre_identity_budget,
            "invalid_chain_budget": self.invalid_chain_budget,
            "schema_envelope_budget": self.schema_envelope_budget,
            "post_identity_loss_budget": self.post_identity_loss_budget,
            "provider_transient_budget": self.provider_transient_budget,
            "pre_identity_used": self.pre_identity_used,
            "invalid_chain_used": self.invalid_chain_used,
            "schema_envelope_used": self.schema_envelope_used,
            "post_identity_loss_used": self.post_identity_loss_used,
            "provider_transient_used": self.provider_transient_used,
            "provider_transient_grant_used": self.provider_transient_grant_used,
            "last_failure": self.last_failure.value if self.last_failure is not None else None,
            "strategy": self.strategy.value,
            "next_eligible_at": self.next_eligible_at,
            "prior_thread_id": self.prior_thread_id,
            "prior_turn_id": self.prior_turn_id,
            "human_attention_reason": self.human_attention_reason,
        }


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """Capability-bound steer/interrupt command persisted by the supervisor."""

    command_id: str
    dispatch_id: DispatchId
    generation: Generation
    attempt: int
    thread_id: ThreadIdentity
    turn_id: str
    kind: ControlCommandKind
    payload: str | None
    payload_sha256: str
    submission_sequence: int = 1
    state: ControlCommandState = ControlCommandState.PENDING
    created_at: str = ""
    sent_at: str | None = None
    acknowledged_at: str | None = None
    acknowledgement: str | None = None
    acknowledgement_terminal_status: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.command_id, label="control command id", limit=256)
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("control command attempt must be positive")
        if not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))
        _required_text(self.turn_id, label="control command turn id", limit=512)
        if any(character.isspace() or ord(character) < 0x20 for character in self.turn_id):
            raise ValueError("control command turn id is malformed")
        if (
            isinstance(self.submission_sequence, bool)
            or not isinstance(self.submission_sequence, int)
            or self.submission_sequence < 1
        ):
            raise ValueError("control command submission sequence must be positive")
        if not isinstance(self.kind, ControlCommandKind):
            object.__setattr__(self, "kind", ControlCommandKind(self.kind))
        if self.kind is ControlCommandKind.STEER:
            if (
                not isinstance(self.payload, str)
                or not self.payload.strip()
                or len(self.payload.encode("utf-8")) > STEER_TEXT_MAX_BYTES
            ):
                raise ValueError("steer command text exceeds its byte limit")
            redacted = redact_control_text(self.payload)
            if redacted != self.payload:
                object.__setattr__(self, "payload", redacted)
        elif self.payload is not None:
            raise ValueError("interrupt command cannot carry text")
        if (
            not isinstance(self.payload_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256) is None
            or hashlib.sha256((self.payload or "").encode("utf-8")).hexdigest() != self.payload_sha256
        ):
            raise ValueError("control command payload digest is invalid")
        if not isinstance(self.state, ControlCommandState):
            object.__setattr__(self, "state", ControlCommandState(self.state))
        if self.created_at:
            _required_text(self.created_at, label="control command timestamp", limit=64)
        if self.sent_at is not None:
            _required_text(self.sent_at, label="control command sent timestamp", limit=64)
        if self.acknowledged_at is not None:
            _required_text(self.acknowledged_at, label="control command acknowledgement timestamp", limit=64)
        if self.acknowledgement is not None:
            _required_text(self.acknowledgement, label="control command acknowledgement", limit=4096)
        if (
            self.acknowledgement_terminal_status is not None
            and self.acknowledgement_terminal_status not in CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES
        ):
            raise ValueError("control command acknowledgement terminal status is invalid")

    def to_json(self) -> JsonObject:
        return {
            "command_id": self.command_id,
            "dispatch_id": str(self.dispatch_id),
            "generation": int(self.generation),
            "attempt": self.attempt,
            "thread_id": self.thread_id.id,
            "turn_id": self.turn_id,
            "kind": self.kind.value,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "submission_sequence": self.submission_sequence,
            "state": self.state.value,
            "created_at": self.created_at,
            "sent_at": self.sent_at,
            "acknowledged_at": self.acknowledged_at,
            "acknowledgement": self.acknowledgement,
            "acknowledgement_terminal_status": self.acknowledgement_terminal_status,
        }


@dataclass(frozen=True, slots=True)
class ControlAcknowledgement:
    """Typed acknowledgement returned for one immutable control command."""

    command_id: str
    dispatch_id: DispatchId
    generation: Generation
    attempt: int
    thread_id: ThreadIdentity
    turn_id: str
    kind: ControlCommandKind
    state: ControlCommandState
    detail: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.command_id, label="control acknowledgement id", limit=256)
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("control acknowledgement attempt must be positive")
        if not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))
        _required_text(self.turn_id, label="control acknowledgement turn id", limit=512)
        if not isinstance(self.kind, ControlCommandKind):
            object.__setattr__(self, "kind", ControlCommandKind(self.kind))
        if not isinstance(self.state, ControlCommandState):
            object.__setattr__(self, "state", ControlCommandState(self.state))
        if self.detail is not None:
            _required_text(self.detail, label="control acknowledgement detail", limit=256)

    def to_json(self) -> JsonObject:
        return {
            "command_id": self.command_id,
            "dispatch_id": str(self.dispatch_id),
            "generation": int(self.generation),
            "attempt": self.attempt,
            "thread_id": self.thread_id.id,
            "turn_id": self.turn_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "detail": self.detail,
        }


class RecoveryActionKind(str, Enum):
    """Authorized durable actions available on a live-worker dispatch."""

    RETRY = "retry"
    CANCEL = "cancel"
    BUDGET_CHANGE = "budget_change"
    COMPATIBILITY_REBIND = "compatibility_rebind"


@dataclass(frozen=True, slots=True)
class CompatibilityRebind:
    """Exact queued installed-runtime identity authorized for bounded recovery."""

    expected_compatibility_sha256: str
    proposed_compatibility_sha256: str
    expected_generation: int
    expected_attempt: int
    expected_profile_sha256: str | None = None
    proposed_profile_sha256: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("expected compatibility digest", self.expected_compatibility_sha256),
            ("proposed compatibility digest", self.proposed_compatibility_sha256),
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} is invalid")
        profile_values = (self.expected_profile_sha256, self.proposed_profile_sha256)
        if (profile_values[0] is None) != (profile_values[1] is None):
            raise ValueError("compatibility rebind profile tuple is incomplete")
        for name, value in (
            ("expected profile digest", self.expected_profile_sha256),
            ("proposed profile digest", self.proposed_profile_sha256),
        ):
            if value is not None and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None):
                raise ValueError(f"{name} is invalid")
        if self.expected_profile_sha256 is not None and (
            self.expected_profile_sha256 == self.proposed_profile_sha256
            and self.expected_compatibility_sha256 == self.proposed_compatibility_sha256
        ):
            raise ValueError("compatibility rebind requires an identity change")
        if self.expected_profile_sha256 is None and (
            self.expected_compatibility_sha256 == self.proposed_compatibility_sha256
        ):
            raise ValueError("compatibility rebind requires an identity change")
        for name, value in (
            ("generation", self.expected_generation),
            ("attempt", self.expected_attempt),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"compatibility rebind {name} is invalid")

    def to_json(self) -> JsonObject:
        payload: JsonObject = {
            "expected_compatibility_sha256": self.expected_compatibility_sha256,
            "proposed_compatibility_sha256": self.proposed_compatibility_sha256,
            "expected_generation": self.expected_generation,
            "expected_attempt": self.expected_attempt,
        }
        # Four-field payloads are retained only to decode immutable v16
        # receipts written before profile identity joined the recovery CAS.
        if self.expected_profile_sha256 is not None:
            payload["expected_profile_sha256"] = self.expected_profile_sha256
            payload["proposed_profile_sha256"] = self.proposed_profile_sha256
        return payload


@dataclass(frozen=True, slots=True)
class RetryBudgetChange:
    """Bounded absolute retry ceilings requested by an authorized action."""

    pre_identity_budget: int
    invalid_chain_budget: int
    schema_envelope_budget: int
    post_identity_loss_budget: int
    # This optional field belongs only to the low-level recovery-control
    # surface.  ``to_json`` intentionally retains the model-facing four-field
    # contract; ``to_control_json`` carries this exact one-step grant without
    # changing controller action semantics.
    provider_transient_budget: int | None = None

    def __post_init__(self) -> None:
        limits = {
            "pre_identity_budget": (self.pre_identity_budget, 5),
            "invalid_chain_budget": (self.invalid_chain_budget, 1),
            "schema_envelope_budget": (self.schema_envelope_budget, 2),
            "post_identity_loss_budget": (self.post_identity_loss_budget, 1),
        }
        for name, (value, maximum) in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} is outside its accepted ceiling")
        if self.provider_transient_budget is not None and (
            isinstance(self.provider_transient_budget, bool)
            or not isinstance(self.provider_transient_budget, int)
            or not 0 <= self.provider_transient_budget <= 4
        ):
            raise ValueError("provider_transient_budget is outside its accepted ceiling")

    def to_json(self) -> JsonObject:
        return {
            "pre_identity_budget": self.pre_identity_budget,
            "invalid_chain_budget": self.invalid_chain_budget,
            "schema_envelope_budget": self.schema_envelope_budget,
            "post_identity_loss_budget": self.post_identity_loss_budget,
        }

    def to_control_json(self) -> JsonObject:
        """Project the existing typed control request, including one grant fact."""

        value = self.to_json()
        if self.provider_transient_budget is not None:
            value["provider_transient_budget"] = self.provider_transient_budget
        return value


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """Immutable record of one compare-and-swap recovery authorization."""

    action_id: str
    dispatch_id: DispatchId
    expected_revision: int
    action_kind: RecoveryActionKind
    reason: str
    requested_budget: RetryBudgetChange | None
    compatibility_rebind: CompatibilityRebind | None
    applied_revision: int
    created_at: str

    def __post_init__(self) -> None:
        _required_text(self.action_id, label="recovery action id", limit=256)
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if (
            isinstance(self.expected_revision, bool)
            or not isinstance(self.expected_revision, int)
            or self.expected_revision < 0
        ):
            raise ValueError("recovery action expected revision is invalid")
        if not isinstance(self.action_kind, RecoveryActionKind):
            object.__setattr__(self, "action_kind", RecoveryActionKind(self.action_kind))
        _required_text(self.reason, label="recovery action reason", limit=512)
        if self.action_kind is RecoveryActionKind.BUDGET_CHANGE and self.requested_budget is None:
            raise ValueError("budget-change action requires requested budgets")
        if self.action_kind is not RecoveryActionKind.BUDGET_CHANGE and self.requested_budget is not None:
            raise ValueError("only budget-change action may carry requested budgets")
        if self.action_kind is RecoveryActionKind.COMPATIBILITY_REBIND and self.compatibility_rebind is None:
            raise ValueError("compatibility-rebind action requires exact compatibility facts")
        if self.action_kind is not RecoveryActionKind.COMPATIBILITY_REBIND and self.compatibility_rebind is not None:
            raise ValueError("only compatibility-rebind action may carry compatibility facts")
        if (
            isinstance(self.applied_revision, bool)
            or not isinstance(self.applied_revision, int)
            or self.applied_revision < 0
        ):
            raise ValueError("recovery action applied revision is invalid")
        _required_text(self.created_at, label="recovery action timestamp", limit=64)

    def to_json(self) -> JsonObject:
        return {
            "action_id": self.action_id,
            "dispatch_id": str(self.dispatch_id),
            "expected_revision": self.expected_revision,
            "action_kind": self.action_kind.value,
            "reason": self.reason,
            "requested_budget": self.requested_budget.to_json() if self.requested_budget is not None else None,
            "compatibility_rebind": (
                self.compatibility_rebind.to_json() if self.compatibility_rebind is not None else None
            ),
            "applied_revision": self.applied_revision,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class LiveWorkerActivity:
    """Typed recent activity response from the supervisor control plane."""

    dispatch_id: DispatchId
    events: tuple[DiagnosticEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if not isinstance(self.events, tuple) or not all(isinstance(event, DiagnosticEvent) for event in self.events):
            raise ValueError("live-worker activity must contain diagnostic events")

    def to_json(self) -> JsonObject:
        return {"dispatch_id": str(self.dispatch_id), "activity": [event.to_json() for event in self.events]}


@dataclass(frozen=True, slots=True)
class LiveWorkerStatus:
    """Closed status projection consumed by clients and later TUI work."""

    dispatch_id: DispatchId
    state: str
    generation: int
    attempt: int
    thread_id: ThreadIdentity | None
    active_turn_id: str | None
    retry_policy: RetryPolicyFacts
    recent_activity: tuple[DiagnosticEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        _required_text(self.state, label="live-worker state", limit=64)
        for name, value in (("generation", self.generation), ("attempt", self.attempt)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"live-worker {name} is invalid")
        if self.thread_id is not None and not isinstance(self.thread_id, ThreadIdentity):
            object.__setattr__(self, "thread_id", ThreadIdentity(self.thread_id))
        if self.active_turn_id is not None:
            _required_text(self.active_turn_id, label="active turn id", limit=512)
        if not isinstance(self.retry_policy, RetryPolicyFacts):
            raise ValueError("live-worker status retry policy is not typed")
        if not isinstance(self.recent_activity, tuple) or not all(
            isinstance(event, DiagnosticEvent) for event in self.recent_activity
        ):
            raise ValueError("live-worker status activity is not typed")

    def to_json(self) -> JsonObject:
        return {
            "dispatch_id": str(self.dispatch_id),
            "state": self.state,
            "generation": self.generation,
            "attempt": self.attempt,
            "thread_id": self.thread_id.id if self.thread_id is not None else None,
            "active_turn_id": self.active_turn_id,
            "retry_policy": self.retry_policy.to_json(),
            "recent_activity": [event.to_json() for event in self.recent_activity],
        }


class CodexFlowError(RuntimeError):
    """Base error for deterministic SDK-boundary failures."""


class TransportFailureBeforeIdentity(CodexFlowError):
    """The SDK failed before a durable thread identity was returned."""


class TransientFailureAfterIdentity(CodexFlowError):
    """A pinned-SDK transient transport/overload failure after identity."""


class TemporaryRateLimitAfterIdentity(TransientFailureAfterIdentity):
    """A typed temporary usage limit with one explicit retry deadline."""

    def __init__(self, message: str, *, retry_at: str) -> None:
        try:
            parsed = datetime.fromisoformat(retry_at.removesuffix("Z") + ("+00:00" if retry_at.endswith("Z") else ""))
        except ValueError as exc:
            raise ValueError("temporary rate-limit deadline is invalid") from exc
        if parsed.tzinfo is None:
            raise ValueError("temporary rate-limit deadline must be timezone-aware")
        super().__init__(message)
        self.retry_at = retry_at


class UnknownSdkFailureBeforeIdentity(TransportFailureBeforeIdentity):
    """An unclassified pre-identity SDK failure, never retryable."""


class TerminalFailureAfterIdentity(CodexFlowError):
    """The SDK reached a thread identity and then failed terminally."""


class UnsupportedCapability(CodexFlowError):
    """A requested optional SDK capability is not exposed by the installed SDK."""


class AuthenticationFailure(CodexFlowError):
    """The SDK rejected authentication; this class is never retryable."""


class PermissionFailure(CodexFlowError):
    """The SDK or native profile rejected permission authority."""


class ProfileFailure(CodexFlowError):
    """The bound native profile was unavailable or incompatible."""


class IntegrityFailure(CodexFlowError):
    """A workspace or durable identity integrity check failed."""


class MalformedInputFailure(CodexFlowError):
    """The SDK rejected malformed input or an unknown non-transient error."""


class UnknownSdkFailureAfterIdentity(TerminalFailureAfterIdentity):
    """An unclassified post-identity SDK failure, never retryable."""


# ---------------------------------------------------------------------------
# H2 durable workflow contracts
# ---------------------------------------------------------------------------


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_LANE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class _ValidatedIdentifier(str):
    """A short, path-safe identifier that can safely cross typed boundaries."""

    label = "identifier"

    def __new__(cls, value: str) -> _ValidatedIdentifier:
        if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{cls.label} must match {_ID_PATTERN.pattern!r}")
        return str.__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


class RunId(_ValidatedIdentifier):
    label = "run id"


class MilestoneId(_ValidatedIdentifier):
    label = "milestone id"


class RoleId(_ValidatedIdentifier):
    label = "role"


class DispatchId(str):
    """Logical dispatch identity: ``run/milestone/role/generation``."""

    label = "dispatch id"

    def __new__(cls, value: str) -> DispatchId:
        if not isinstance(value, str):
            raise ValueError(f"{cls.label} must be a string")
        parts = value.split("/")
        if len(parts) != 4:
            raise ValueError("dispatch id must be <run>/<milestone>/<role>/<generation>")
        RunId(parts[0])
        MilestoneId(parts[1])
        RoleId(parts[2])
        Generation(parts[3])
        return str.__new__(cls, value)

    @classmethod
    def from_parts(
        cls,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        role: RoleId | str,
        generation: Generation | int | str,
    ) -> DispatchId:
        run = RunId(run_id)
        milestone = MilestoneId(milestone_id)
        role_value = RoleId(role)
        generation_value = Generation(generation)
        return cls(f"{run}/{milestone}/{role_value}/{generation_value}")

    @property
    def value(self) -> str:
        return str(self)

    @property
    def parts(self) -> tuple[RunId, MilestoneId, RoleId, Generation]:
        run, milestone, role, generation = str(self).split("/")
        return RunId(run), MilestoneId(milestone), RoleId(role), Generation(generation)


class EventId(str):
    """Stable event identity, scoped to a milestone sequence."""

    def __new__(cls, value: str) -> EventId:
        if not isinstance(value, str) or len(value) > 256 or not value.strip():
            raise ValueError("event id must be a non-empty bounded string")
        if any(character.isspace() for character in value):
            raise ValueError("event id must not contain whitespace")
        return str.__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


class Generation(int):
    """Positive dispatch generation; generations never silently increment."""

    def __new__(cls, value: int | str) -> Generation:
        if isinstance(value, bool):
            raise ValueError("generation must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("generation must be a positive integer") from exc
        if str(parsed) != str(value) and not isinstance(value, int):
            raise ValueError("generation must be a canonical integer")
        if parsed < 1:
            raise ValueError("generation must be a positive integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class EventSequence(int):
    """Positive monotonically increasing sequence within one milestone."""

    def __new__(cls, value: int | str) -> EventSequence:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("event sequence must be a positive integer") from exc
        if isinstance(value, bool) or parsed < 1:
            raise ValueError("event sequence must be a positive integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class SchemaVersion(int):
    """Version marker for the SQLite schema."""

    def __new__(cls, value: int | str) -> SchemaVersion:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("schema version must be a non-negative integer") from exc
        if isinstance(value, bool) or parsed < 0:
            raise ValueError("schema version must be a non-negative integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class WorkflowState(str, Enum):
    PLANNED = "PLANNED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    NEEDS_DECISION = "NEEDS_DECISION"
    COMPLETED = "COMPLETED"
    REVIEWING = "REVIEWING"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AcceptanceMode(str, Enum):
    """Independent acceptance authorities a milestone may require."""

    OBJECTIVE = "objective"
    VISUAL = "visual"
    ARCHITECTURE = "architecture"


@dataclass(frozen=True, slots=True)
class RenderedEvidence:
    """Fixed, review-only evidence for a subjective acceptance mode.

    Rendered evidence is intentionally represented by a digest and bounded
    metadata rather than raw pixels.  The producer owns the actual artifact;
    reviewers receive this immutable identity and can never write through the
    Review workflow controller callback.
    """

    evidence_id: str
    revision: str
    artifact_sha256: str
    artifact_path: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.evidence_id, label="rendered evidence id", limit=256)
        _required_text(self.revision, label="rendered evidence revision", limit=256)
        _required_text(self.artifact_path, label="rendered evidence path", limit=512)
        evidence_path = Path(self.artifact_path)
        if evidence_path.is_absolute() or ".." in evidence_path.parts or evidence_path.as_posix() != self.artifact_path:
            raise ValueError("rendered evidence path must be repository-relative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256):
            raise ValueError("rendered evidence digest must be a lowercase SHA-256")
        for label, value in (("width", self.width), ("height", self.height)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"rendered evidence {label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class AuthorityAssignment:
    """One concrete role route selected for a required acceptance authority."""

    mode: AcceptanceMode | None
    role: RoleId
    model: str
    reasoning_effort: ReasoningEffort

    def __post_init__(self) -> None:
        if self.mode is not None and not isinstance(self.mode, AcceptanceMode):
            object.__setattr__(self, "mode", AcceptanceMode(self.mode))
        if not isinstance(self.role, RoleId):
            object.__setattr__(self, "role", RoleId(self.role))
        if not isinstance(self.model, str) or not self.model.strip() or "\x00" in self.model:
            raise ValueError("authority model must be a bounded non-empty string")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            object.__setattr__(self, "reasoning_effort", ReasoningEffort(self.reasoning_effort))


@dataclass(frozen=True, slots=True)
class AuthorityPlan:
    """The immutable routing decision for one multi-authority review run."""

    assignments: tuple[AuthorityAssignment, ...]

    def __post_init__(self) -> None:
        assignments = tuple(self.assignments)
        roles = [str(item.role) for item in assignments]
        if len(roles) != len(set(roles)):
            raise ValueError("required review authorities must use distinct roles")
        modes = [item.mode for item in assignments if item.mode is not None]
        if len(modes) != len(set(modes)):
            raise ValueError("required review acceptance modes must use distinct authorities")
        object.__setattr__(self, "assignments", assignments)

    def for_mode(self, mode: AcceptanceMode | str) -> AuthorityAssignment:
        target = mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode)
        for assignment in self.assignments:
            if assignment.mode is target:
                return assignment
        raise KeyError(target)

    def role(self, role: RoleId | str) -> AuthorityAssignment:
        target = str(role)
        for assignment in self.assignments:
            if str(assignment.role) == target:
                return assignment
        raise KeyError(target)


class FindingCausalClass(str, Enum):
    """Causal classes used to diagnose a review finding without attempt counts."""

    IMPLEMENTATION = "implementation"
    CONTRACT = "contract"
    ACCEPTANCE = "acceptance"
    SECURITY = "security"
    ENVIRONMENT = "environment"
    TRANSPORT = "transport"
    REVIEW = "review"
    STALE_REVIEW = "stale_review"


class Severity(str, Enum):
    """Severity is intentionally independent from successor promotion impact."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# Short aliases keep the public contract readable for callers that use the
# plan's prose names rather than the longer enum class names.
CausalClass = FindingCausalClass
FindingSeverity = Severity


class RecoveryOutcome(str, Enum):
    """Typed recovery outcomes; terminal and non-terminal meanings are distinct."""

    FINISH_LOCALLY = "finish_locally"
    CHANGE_STRATEGY = "change_strategy"
    CONTINUE_WITH_REPLAN = "continue_with_replan"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"


class LifecyclePhase(str, Enum):
    """Durable review workflow phases."""

    EXECUTION = "execution"
    REVIEW = "review"
    REPAIR = "repair"
    RECOVERY = "recovery"
    DECISION = "decision"
    BUDGET = "budget"
    ACCEPTANCE = "acceptance"
    ROUTING = "routing"


class BudgetExhaustion(str, Enum):
    """Which bounded resource stopped another external turn."""

    TURNS = "turns"
    REPAIRS = "repairs"
    REVIEWS = "reviews"
    COMPACTIONS = "compactions"
    VALIDATIONS = "validations"
    WALL_CLOCK = "wall_clock"


class LifecycleStatus(str, Enum):
    """Observable result labels kept distinct from recovery decisions."""

    ACCEPTED = "accepted"
    CONTINUE_WITH_REPLAN = "continue_with_replan"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"
    CONTEXT_ROLLOVER = "context_rollover"
    TRANSPORT_FAILURE = "transport_failure"
    STALE_REVIEW = "stale_review"
    LIMIT_EXHAUSTED = "limit_exhausted"
    REJECTED_AFTER_REPAIR = "rejected_after_repair"
    STALE_AUTHORITY = "stale_authority"
    ROUTE_UNAVAILABLE = "route_unavailable"
    NOTIFICATION_FAILURE = "notification_failure"


TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.ACCEPTED, WorkflowState.BLOCKED, WorkflowState.FAILED, WorkflowState.CANCELLED}
)


def coerce_state(value: WorkflowState | str) -> WorkflowState:
    if isinstance(value, WorkflowState):
        return value
    try:
        return WorkflowState(value)
    except ValueError as exc:
        raise ValueError(f"unknown workflow state: {value!r}") from exc


def is_transition_allowed(from_state: WorkflowState | str, to_state: WorkflowState | str) -> bool:
    """Return the single canonical H2 transition-policy decision."""

    source = coerce_state(from_state)
    target = coerce_state(to_state)
    match source:
        case WorkflowState.PLANNED:
            return target in {
                WorkflowState.STARTING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.STARTING | WorkflowState.NEEDS_DECISION | WorkflowState.REPAIR_REQUIRED:
            return target in {
                WorkflowState.RUNNING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.RUNNING:
            return target in {
                WorkflowState.NEEDS_DECISION,
                WorkflowState.COMPLETED,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.COMPLETED:
            return target in {
                WorkflowState.REVIEWING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.REVIEWING:
            return target in {
                WorkflowState.REPAIR_REQUIRED,
                WorkflowState.ACCEPTED,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.ACCEPTED | WorkflowState.BLOCKED | WorkflowState.FAILED | WorkflowState.CANCELLED:
            return False
    raise AssertionError(f"unhandled workflow state: {source!r}")


ALLOWED_TRANSITIONS: Mapping[WorkflowState, frozenset[WorkflowState]] = MappingProxyType(
    {
        source: frozenset(target for target in WorkflowState if is_transition_allowed(source, target))
        for source in WorkflowState
    }
)


class ReasonCode(str, Enum):
    """Stable reason categories owned by the controller."""

    DECISION_REQUIRED = "decision_required"
    ACCEPTANCE_AMBIGUITY = "acceptance_ambiguity"
    CONTRACT_CHANGE = "contract_change"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    REVIEW_REJECTED = "review_rejected"
    CONTEXT_ROLLOVER = "context_rollover"
    TRANSPORT_FAILURE = "transport_failure"
    EXECUTION_FAILURE = "execution_failure"
    TERMINAL_OUTCOME = "terminal_outcome"
    DISPATCH_CLAIMED = "dispatch_claimed"


@dataclass(frozen=True, slots=True)
class WorkflowReason:
    code: ReasonCode
    detail: None = None

    def __post_init__(self) -> None:
        if self.detail is not None:
            raise ValueError("durable workflow reasons do not carry free-form detail")


class PreIdentityTransportFailure(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.TRANSPORT_FAILURE)


class PostIdentityExecutionFailure(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.EXECUTION_FAILURE)


class ReviewRejected(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.REVIEW_REJECTED)


class TerminalOutcome(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.TERMINAL_OUTCOME)


def _required_text(value: str, *, label: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _owned_json_object(value: Mapping[str, object], *, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        frozen = freeze_json(dict(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must contain interoperable JSON") from exc
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise ValueError(f"{label} must be a JSON object")
    return cast(JsonObject, frozen)


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """A stable, independently reviewable finding."""

    finding_id: str
    causal_class: FindingCausalClass
    severity: Severity
    promotion_blocking: bool
    promotion_reason: str
    evidence: JsonObject
    criterion: str
    defer_to: str | None = None
    survives_prior_repair: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or _ID_PATTERN.fullmatch(self.finding_id) is None:
            raise ValueError("finding id must be a stable bounded identifier")
        if not isinstance(self.causal_class, FindingCausalClass):
            raise ValueError("finding causal class must be typed")
        if not isinstance(self.severity, Severity):
            raise ValueError("finding severity must be typed")
        if not isinstance(self.promotion_blocking, bool):
            raise ValueError("promotion_blocking must be boolean")
        _required_text(self.promotion_reason, label="promotion reason")
        _required_text(self.criterion, label="acceptance criterion")
        if self.defer_to is not None:
            _required_text(self.defer_to, label="defer_to", limit=256)
        object.__setattr__(self, "evidence", _owned_json_object(self.evidence, label="finding evidence"))

    @property
    def blocks_successor(self) -> bool:
        return self.promotion_blocking

    @property
    def id(self) -> str:
        return self.finding_id

    @property
    def reason(self) -> str:
        return self.promotion_reason

    @property
    def promotion_impact(self) -> bool:
        return self.promotion_blocking


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Read-only reviewer output whose freshness is explicit and durable."""

    review_id: str
    reviewer_role: RoleId
    accepted: bool
    findings: tuple[ReviewFinding, ...]
    reviewed_revision: str
    fresh: bool = True
    read_only: bool = True
    prior_review_id: str | None = None
    acceptance_mode: AcceptanceMode = AcceptanceMode.OBJECTIVE
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.review_id, label="review id", limit=256)
        _required_text(self.reviewed_revision, label="reviewed revision", limit=256)
        if not isinstance(self.reviewer_role, RoleId):
            object.__setattr__(self, "reviewer_role", RoleId(self.reviewer_role))
        findings = tuple(self.findings)
        if len({finding.finding_id for finding in findings}) != len(findings):
            raise ValueError("review findings must have unique stable identities")
        if not self.read_only:
            raise ValueError("review result must declare read-only authority")
        if self.accepted and any(finding.promotion_blocking for finding in findings):
            raise ValueError("accepted review cannot contain promotion-blocking findings")
        if not isinstance(self.acceptance_mode, AcceptanceMode):
            object.__setattr__(self, "acceptance_mode", AcceptanceMode(self.acceptance_mode))
        evidence_ids = tuple(self.evidence_ids)
        if any(not isinstance(value, str) or not value.strip() for value in evidence_ids):
            raise ValueError("review evidence ids must be non-empty strings")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "findings", findings)

    @property
    def promotion_blockers(self) -> tuple[ReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.promotion_blocking)


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Durable repair acknowledgement, normally owned by the original executor."""

    repair_id: str
    finding_id: str
    owner_dispatch_id: DispatchId
    action: str
    outcome: str
    same_owner: bool = True
    prior_finding_survives: bool = False

    def __post_init__(self) -> None:
        _required_text(self.repair_id, label="repair id", limit=256)
        if not isinstance(self.finding_id, str) or not self.finding_id:
            raise ValueError("repair finding id must be non-empty")
        _required_text(self.action, label="repair action")
        _required_text(self.outcome, label="repair outcome")
        if not isinstance(self.owner_dispatch_id, DispatchId):
            object.__setattr__(self, "owner_dispatch_id", DispatchId(self.owner_dispatch_id))
        if self.same_owner is not True:
            raise ValueError("review repairs must remain with the durable owner")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """A typed diagnosis; only the three terminal outcomes are user-visible."""

    outcome: RecoveryOutcome
    rationale: str
    checkpoint: str
    finding_ids: tuple[str, ...] = ()
    external_prerequisite: str | None = None
    decision_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RecoveryOutcome):
            object.__setattr__(self, "outcome", RecoveryOutcome(self.outcome))
        _required_text(self.rationale, label="recovery rationale")
        _required_text(self.checkpoint, label="recovery checkpoint", limit=256)
        if self.outcome is RecoveryOutcome.EXTERNAL_BLOCKED and not self.external_prerequisite:
            raise ValueError("external-blocked recovery requires an actionable prerequisite")
        if self.outcome is RecoveryOutcome.NEEDS_DECISION and not self.decision_request_id:
            raise ValueError("needs-decision recovery requires a durable decision request")
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))


@dataclass(frozen=True, slots=True)
class ReplanProposal:
    """Bounded architecture recovery proposal.

    The controller compares the declared boundary flags before it permits the
    executor to continue.  A proposal is therefore an implementation strategy
    change, never an implicit change to user intent or public authority.
    """

    strategy: str
    intent_unchanged: bool = True
    contract_unchanged: bool = True
    security_boundary_unchanged: bool = True
    cost_unchanged: bool = True
    destructive_behavior_unchanged: bool = True
    scope_unchanged: bool = True

    def __post_init__(self) -> None:
        _required_text(self.strategy, label="revised strategy")
        flags = (
            self.intent_unchanged,
            self.contract_unchanged,
            self.security_boundary_unchanged,
            self.cost_unchanged,
            self.destructive_behavior_unchanged,
            self.scope_unchanged,
        )
        if any(not isinstance(flag, bool) for flag in flags):
            raise ValueError("replan boundary flags must be boolean")

    @property
    def boundary_unchanged(self) -> bool:
        return all(
            (
                self.intent_unchanged,
                self.contract_unchanged,
                self.security_boundary_unchanged,
                self.cost_unchanged,
                self.destructive_behavior_unchanged,
                self.scope_unchanged,
            )
        )


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Durable user decision request; notification is never authoritative."""

    request_id: str
    question: str
    options: tuple[str, ...]
    blocking: bool = True

    def __post_init__(self) -> None:
        _required_text(self.request_id, label="decision request id", limit=256)
        _required_text(self.question, label="decision question")
        options = tuple(_required_text(option, label="decision option", limit=512) for option in self.options)
        if not options or len(set(options)) != len(options):
            raise ValueError("decision request options must be unique and non-empty")
        object.__setattr__(self, "options", options)


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """A validated response to a previously persisted decision request."""

    request_id: str
    choice: str
    rationale: str

    def __post_init__(self) -> None:
        _required_text(self.request_id, label="decision request id", limit=256)
        _required_text(self.choice, label="decision choice", limit=512)
        _required_text(self.rationale, label="decision rationale")


@dataclass(frozen=True, slots=True)
class Budget:
    """Bounded resource usage with fail-closed checks before external turns."""

    max_turns: int = 1
    max_repairs: int = 1
    max_reviews: int = 2
    max_compactions: int = 1
    max_validations: int = 2
    wall_clock_seconds: float = 900.0
    turns: int = 0
    repairs: int = 0
    reviews: int = 0
    compactions: int = 0
    validations: int = 0
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        for name in ("max_turns", "max_repairs", "max_reviews", "max_compactions", "max_validations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.wall_clock_seconds) or self.wall_clock_seconds <= 0:
            raise ValueError("wall_clock_seconds must be positive")
        for name in ("turns", "repairs", "reviews", "compactions", "validations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")

    def exhausted(self) -> BudgetExhaustion | None:
        checks = (
            (self.turns, self.max_turns, BudgetExhaustion.TURNS),
            (self.repairs, self.max_repairs, BudgetExhaustion.REPAIRS),
            (self.reviews, self.max_reviews, BudgetExhaustion.REVIEWS),
            (self.compactions, self.max_compactions, BudgetExhaustion.COMPACTIONS),
            (self.validations, self.max_validations, BudgetExhaustion.VALIDATIONS),
        )
        for used, limit, kind in checks:
            if used >= limit:
                return kind
        if self.elapsed_seconds >= self.wall_clock_seconds:
            return BudgetExhaustion.WALL_CLOCK
        return None


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """One durable review fact projected from authoritative ledger events."""

    phase: LifecyclePhase
    kind: str
    sequence: int
    data: JsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.phase, LifecyclePhase):
            object.__setattr__(self, "phase", LifecyclePhase(self.phase))
        _required_text(self.kind, label="lifecycle kind", limit=128)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("lifecycle sequence must be a positive integer")
        object.__setattr__(self, "data", _owned_json_object(self.data, label="lifecycle data"))


@dataclass(frozen=True, slots=True)
class ReviewLifecycleResult:
    """Observable terminal review-workflow result."""

    status: LifecycleStatus | str
    accepted: bool
    review_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    repair_ids: tuple[str, ...]
    lifecycle: tuple[LifecycleRecord, ...]
    recovery: RecoveryDecision | None = None
    authority_plan: AuthorityPlan | None = None
    rendered_evidence: tuple[RenderedEvidence, ...] = ()
    notification_failure: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, LifecycleStatus):
            try:
                object.__setattr__(self, "status", LifecycleStatus(self.status))
            except ValueError as exc:
                raise ValueError("unknown review lifecycle status") from exc
        object.__setattr__(self, "review_ids", tuple(self.review_ids))
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))
        object.__setattr__(self, "repair_ids", tuple(self.repair_ids))
        object.__setattr__(self, "lifecycle", tuple(self.lifecycle))
        object.__setattr__(self, "rendered_evidence", tuple(self.rendered_evidence))
        if not isinstance(self.notification_failure, bool):
            raise ValueError("notification_failure must be boolean")


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: RunId
    created_at: str
    closed_at: str | None
    metadata: JsonObject


@dataclass(frozen=True, slots=True)
class MilestoneRecord:
    run_id: RunId
    milestone_id: MilestoneId
    state: WorkflowState
    created_at: str
    updated_at: str
    metadata: JsonObject


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    dispatch_id: DispatchId
    run_id: RunId
    milestone_id: MilestoneId
    role: RoleId
    generation: Generation
    claimed_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: EventId
    run_id: RunId
    milestone_id: MilestoneId
    sequence: EventSequence
    from_state: WorkflowState | None
    to_state: WorkflowState
    event_type: str
    reason: WorkflowReason | None
    occurred_at: str
    dispatch_id: DispatchId | None = None
    data: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class RecoveryFact:
    dispatch: DispatchClaim
    state: WorkflowState
    last_event: EventRecord


# ---------------------------------------------------------------------------
# H6-G controller decision and recovery contracts
# ---------------------------------------------------------------------------


class ControllerDecisionId(str):
    """Stable identity of one durable controller wake decision."""

    def __new__(cls, value: str) -> ControllerDecisionId:
        if not isinstance(value, str) or not value.startswith("decision/"):
            raise ValueError("controller decision id must start with decision/")
        if len(value.encode("utf-8")) > 512 or any(c.isspace() or ord(c) < 0x20 for c in value):
            raise ValueError("controller decision id is invalid")
        return str.__new__(cls, value)


class ControllerDecisionState(str, Enum):
    PENDING_DELIVERY = "pending_delivery"
    AWAITING_CLAIM = "awaiting_claim"
    CLAIMED = "claimed"
    ACTION_COMMITTED = "action_committed"
    ACKNOWLEDGED = "acknowledged"
    SUPERSEDED = "superseded"
    HUMAN_ATTENTION_REQUIRED = "human_attention_required"
    LEGACY_CLOSED = "legacy_closed"


class ControllerClaimantKind(str, Enum):
    MODEL = "model"
    HUMAN = "human"


class ControllerGenerationState(str, Enum):
    PREPARED = "prepared"
    DELIVERY_STARTING = "delivery_starting"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    SUPERSEDED = "superseded"


class ControllerActionKind(str, Enum):
    ACKNOWLEDGE_ONLY = "acknowledge_only"
    REARM_CHECKPOINT = "rearm_checkpoint"
    RETRY_DISPATCH = "retry_dispatch"
    CANCEL_DISPATCH = "cancel_dispatch"
    CHANGE_RETRY_BUDGET = "change_retry_budget"
    REQUIRE_HUMAN_ATTENTION = "require_human_attention"


@dataclass(frozen=True, slots=True)
class ControllerDecisionSummary:
    """Bounded, secretless context presented to a controller generation."""

    dispatch_id: DispatchId
    kind: str
    state: ControllerDecisionState
    summary: str
    source_thread_id: str | None = None
    deadline: str | None = None
    revision: int = 0
    expected_successor_dispatch_ids: tuple[str, ...] = ()
    dispatch_state: str = "unknown"
    retry_policy_revision: int = 0
    retry_strategy: RecoveryStrategy = RecoveryStrategy.NONE
    last_failure: RetryFailureClass | None = None
    human_attention_reason: str | None = None
    prior_thread_id: ThreadIdentity | None = None
    prior_turn_id: str | None = None
    recovery_state: str = "none"
    worker_exit_classification: str | None = None
    worker_exit_code: int | None = None
    recent_activity: tuple[DiagnosticEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if self.kind not in {"checkpoint", "terminal"}:
            raise ValueError("controller decision kind is invalid")
        if not isinstance(self.state, ControllerDecisionState):
            object.__setattr__(self, "state", ControllerDecisionState(self.state))
        _required_text(self.summary, label="controller decision summary", limit=16_384)
        if self.source_thread_id is not None:
            ThreadIdentity(self.source_thread_id)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("controller decision revision is invalid")
        successors = tuple(self.expected_successor_dispatch_ids)
        if len(successors) > 128 or len(set(successors)) != len(successors):
            raise ValueError("controller successor snapshot is invalid")
        for successor in successors:
            DispatchId(successor)
        object.__setattr__(self, "expected_successor_dispatch_ids", tuple(sorted(successors)))
        _required_text(self.dispatch_state, label="controller dispatch state", limit=64)
        if (
            isinstance(self.retry_policy_revision, bool)
            or not isinstance(self.retry_policy_revision, int)
            or self.retry_policy_revision < 0
        ):
            raise ValueError("controller retry-policy revision is invalid")
        if not isinstance(self.retry_strategy, RecoveryStrategy):
            object.__setattr__(self, "retry_strategy", RecoveryStrategy(self.retry_strategy))
        if self.last_failure is not None and not isinstance(self.last_failure, RetryFailureClass):
            object.__setattr__(self, "last_failure", RetryFailureClass(self.last_failure))
        if self.human_attention_reason is not None:
            object.__setattr__(
                self,
                "human_attention_reason",
                redact_control_text(self.human_attention_reason, limit=512),
            )
        if self.prior_thread_id is not None and not isinstance(self.prior_thread_id, ThreadIdentity):
            object.__setattr__(self, "prior_thread_id", ThreadIdentity(self.prior_thread_id))
        if self.prior_turn_id is not None:
            _required_text(self.prior_turn_id, label="controller prior turn id", limit=512)
        _required_text(self.recovery_state, label="controller recovery state", limit=64)
        if self.worker_exit_classification is not None:
            _required_text(
                self.worker_exit_classification,
                label="controller worker-exit classification",
                limit=128,
            )
        if self.worker_exit_code is not None and (
            isinstance(self.worker_exit_code, bool) or not isinstance(self.worker_exit_code, int)
        ):
            raise ValueError("controller worker-exit code is invalid")
        activity = tuple(self.recent_activity)
        if len(activity) > 8 or not all(isinstance(event, DiagnosticEvent) for event in activity):
            raise ValueError("controller recent activity snapshot is invalid")
        object.__setattr__(self, "recent_activity", activity)

    def context_json(self) -> JsonObject:
        """Return the bounded recovery facts a model needs for one CAS decision."""

        return {
            "dispatch_state": self.dispatch_state,
            "retry_policy_revision": self.retry_policy_revision,
            "retry_strategy": self.retry_strategy.value,
            "last_failure": self.last_failure.value if self.last_failure is not None else None,
            "human_attention_reason": self.human_attention_reason,
            "prior_thread_id": self.prior_thread_id.id if self.prior_thread_id is not None else None,
            "prior_turn_id": self.prior_turn_id,
            "recovery_state": self.recovery_state,
            "worker_exit_classification": self.worker_exit_classification,
            "worker_exit_code": self.worker_exit_code,
            "recent_activity": [event.to_json() for event in self.recent_activity],
        }


@dataclass(frozen=True, slots=True)
class ControllerDecisionClaim:
    decision_id: ControllerDecisionId
    generation: Generation
    claimant_kind: ControllerClaimantKind
    claimant_id: str
    revision: int
    lease_expires_at: str
    token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        if not isinstance(self.claimant_kind, ControllerClaimantKind):
            object.__setattr__(self, "claimant_kind", ControllerClaimantKind(self.claimant_kind))
        _required_text(self.claimant_id, label="controller claimant id", limit=256)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("controller claim revision is invalid")
        _required_text(self.lease_expires_at, label="controller claim lease", limit=64)
        if self.token is not None:
            _required_text(self.token, label="controller claim token", limit=256)


@dataclass(frozen=True, slots=True)
class ControllerRecoveryInspectionClaim:
    """The once-returned capability for one authoritative recovery read."""

    decision_id: ControllerDecisionId
    generation: Generation
    revision: int
    lease_expires_at: str
    token: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("controller inspection claim revision is invalid")
        _required_text(self.lease_expires_at, label="controller inspection lease", limit=64)
        _required_text(self.token, label="controller inspection token", limit=256)

    @property
    def inspection_token(self) -> str:
        return self.token

    @property
    def expires_at(self) -> str:
        return self.lease_expires_at


@dataclass(frozen=True, slots=True)
class ControllerDecisionStatus:
    decision_id: ControllerDecisionId
    dispatch_id: DispatchId
    kind: str
    state: ControllerDecisionState
    revision: int
    current_generation: Generation
    generation_budget: int
    generation_used: int
    claimant_kind: ControllerClaimantKind | None
    claimant_id: str | None
    claim_expires_at: str | None
    action_id: str | None
    action_sha256: str | None
    deadline: str
    summary: ControllerDecisionSummary

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if not isinstance(self.state, ControllerDecisionState):
            object.__setattr__(self, "state", ControllerDecisionState(self.state))
        if not isinstance(self.current_generation, Generation):
            object.__setattr__(self, "current_generation", Generation(self.current_generation))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("controller decision revision is invalid")
        if isinstance(self.generation_budget, bool) or not 1 <= self.generation_budget <= 2:
            raise ValueError("controller generation budget is invalid")
        if isinstance(self.generation_used, bool) or not 0 <= self.generation_used <= self.generation_budget:
            raise ValueError("controller generation usage is invalid")
        if self.claimant_kind is not None and not isinstance(self.claimant_kind, ControllerClaimantKind):
            object.__setattr__(self, "claimant_kind", ControllerClaimantKind(self.claimant_kind))
        if self.claimant_id is not None:
            _required_text(self.claimant_id, label="controller claimant id", limit=256)
        _required_text(self.deadline, label="controller decision deadline", limit=64)


@dataclass(frozen=True, slots=True)
class ControllerGenerationStatus:
    decision_id: ControllerDecisionId
    generation: Generation
    lineage_id: str
    state: ControllerGenerationState
    predecessor_generation: Generation | None = None
    source_kind: str = "source_controller"
    prompt_sha256: str | None = None
    controller_thread_id: ThreadIdentity | None = None
    controller_turn_id: str | None = None
    inspection_outcome: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        _required_text(self.lineage_id, label="controller lineage id", limit=256)
        if not isinstance(self.state, ControllerGenerationState):
            object.__setattr__(self, "state", ControllerGenerationState(self.state))
        if self.predecessor_generation is not None and not isinstance(self.predecessor_generation, Generation):
            object.__setattr__(self, "predecessor_generation", Generation(self.predecessor_generation))
        if self.source_kind not in {"source_controller", "replacement_controller"}:
            raise ValueError("controller generation source kind is invalid")
        if self.prompt_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256) is None:
            raise ValueError("controller prompt digest is invalid")
        if self.controller_thread_id is not None and not isinstance(self.controller_thread_id, ThreadIdentity):
            object.__setattr__(self, "controller_thread_id", ThreadIdentity(self.controller_thread_id))
        if self.controller_turn_id is not None:
            _required_text(self.controller_turn_id, label="controller turn id", limit=512)


@dataclass(frozen=True, slots=True)
class ControllerActionReceipt:
    action_id: str
    decision_id: ControllerDecisionId
    generation: Generation
    expected_revision: int
    bundle_sha256: str
    effect_receipt: JsonObject
    state: str
    committed_at: str
    acknowledged_at: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.action_id, label="controller action id", limit=256)
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        if (
            isinstance(self.expected_revision, bool)
            or not isinstance(self.expected_revision, int)
            or self.expected_revision < 0
        ):
            raise ValueError("controller action revision is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.bundle_sha256) is None:
            raise ValueError("controller bundle digest is invalid")
        if self.state not in {"committed", "acknowledged"}:
            raise ValueError("controller action receipt state is invalid")
        _required_text(self.committed_at, label="controller action timestamp", limit=64)


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    run: RunRecord
    milestones: tuple[MilestoneRecord, ...]
    dispatches: tuple[DispatchClaim, ...]
    events: tuple[EventRecord, ...]


# ---------------------------------------------------------------------------
# H3 execution workspace and controller contracts
# ---------------------------------------------------------------------------


class WorkspaceMode(str, Enum):
    CURRENT_CHECKOUT = "current_checkout"
    EXISTING_WORKTREE = "existing_worktree"
    MANAGED_WORKTREE = "managed_worktree"


class ExecutionStatus(str, Enum):
    PLANNED = "planned"
    THREAD_STARTED = "thread_started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN_PRE_IDENTITY = "uncertain_pre_identity"


class ControllerCheckpoint(str, Enum):
    CAPSULE_PLANNED = "capsule_planned"
    WORKSPACE_LEASED = "workspace_leased"
    WORKSPACE_BASELINE_DURABLE = "workspace_baseline_durable"
    THREAD_STARTING = "thread_starting"
    THREAD_IDENTITY_DURABLE = "thread_identity_durable"
    TURN_DURABLE = "turn_durable"
    VALIDATION_DURABLE = "validation_durable"
    RESULT_DURABLE = "result_durable"


class ValidationFailureCode(str, Enum):
    """Controller-owned, non-sensitive validation failure categories."""

    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    INTEGRITY_FAILURE = "integrity_failure"


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    argv: tuple[str, ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item or "\x00" in item for item in self.argv):
            raise ValueError("validation argv must contain non-empty strings")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("validation timeout must be positive")


_SCHEMA_TYPES = frozenset({"object", "array", "string", "boolean", "number", "integer", "null"})
_SCHEMA_METADATA_KEYS = frozenset({"$schema", "$id", "title", "description"})
_SCHEMA_KEYS = _SCHEMA_METADATA_KEYS | frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "items",
        "const",
        "enum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "multipleOf",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "oneOf",
    }
)
MAX_OUTPUT_SCHEMA_DEPTH = 32
MAX_OUTPUT_SCHEMA_PROPERTIES = 1_024
MAX_OUTPUT_OBJECT_PROPERTIES = 128
MAX_OUTPUT_PROPERTY_NAME_BYTES = 256
MAX_OUTPUT_ARRAY_ITEMS = 1_024
MAX_STRUCTURED_OUTPUT_BYTES = 1_048_576


def _is_closed_constant_map(node: Mapping[str, object], properties: Mapping[str, object]) -> bool:
    """Identify a bounded supported-key map whose values are fixed per key."""

    return (
        "required" not in node
        and "minProperties" in node
        and "maxProperties" in node
        and bool(properties)
        and all(isinstance(child, Mapping) and "const" in child for child in properties.values())
    )


def _is_finite_json_number(value: object) -> bool:
    """Return whether a value is a finite JSON number without coercing integers."""

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _json_schema_instance_equal(left: object, right: object) -> bool:
    """Compare two JSON instances using Draft 2020-12 equality semantics."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_finite_json_number(left) and _is_finite_json_number(right):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping) or set(left) != set(right):
            return False
        return all(isinstance(key, str) and _json_schema_instance_equal(left[key], right[key]) for key in left)
    if isinstance(left, Sequence) or isinstance(right, Sequence):
        if (
            not isinstance(left, Sequence)
            or not isinstance(right, Sequence)
            or isinstance(left, str | bytes | bytearray)
            or isinstance(right, str | bytes | bytearray)
            or len(left) != len(right)
        ):
            return False
        return all(
            _json_schema_instance_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return False


def validate_output_schema(schema: Mapping[str, object]) -> None:
    """Validate the one closed recursive schema contract accepted at every boundary."""

    properties_seen = 0

    def bounded_integer(value: object, *, label: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise ValueError(f"{label} must be a bounded non-negative integer")
        return value

    def value_matches_type(value: object, schema_type: str) -> bool:
        if schema_type == "null":
            return value is None
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            return _is_finite_json_number(value)
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "array":
            return isinstance(value, list | tuple)
        return isinstance(value, Mapping)

    def validate_metadata(node: Mapping[str, object]) -> None:
        for key in _SCHEMA_METADATA_KEYS & set(node):
            value = node[key]
            if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > 16_384:
                raise ValueError(f"output schema {key} must be a bounded non-empty string")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("output schema $schema must identify Draft 2020-12")
        if "description" in node and not {"$schema", "$id", "title"} <= set(node):
            raise ValueError("output schema description is permitted only with canonical schema metadata")

    def validate_node(node: object, *, depth: int, root: bool) -> None:
        nonlocal properties_seen
        if not isinstance(node, Mapping):
            raise ValueError("output schema nodes must be objects")
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise ValueError("output schema exceeds the depth limit")
        unknown = set(node) - _SCHEMA_KEYS
        if unknown:
            raise ValueError(f"output schema contains unsupported keys: {sorted(map(str, unknown))!r}")
        validate_metadata(node)
        if "oneOf" in node:
            branches = node["oneOf"]
            if (
                set(node) - _SCHEMA_METADATA_KEYS != {"oneOf"}
                or not isinstance(branches, list | tuple)
                or not (2 <= len(branches) <= (3 if root else 8))
                or any(not isinstance(branch, Mapping) for branch in branches)
            ):
                raise ValueError("output schema oneOf must be a single bounded branch authority")
            for branch in branches:
                validate_node(branch, depth=depth + 1, root=root)
            return
        raw_type = node.get("type")
        if isinstance(raw_type, str):
            schema_types = (raw_type,)
        elif isinstance(raw_type, list | tuple) and all(isinstance(item, str) for item in raw_type):
            schema_types = tuple(raw_type)
        elif "const" in node:
            constant = node["const"]
            inferred = (
                "null"
                if constant is None
                else "boolean"
                if isinstance(constant, bool)
                else "integer"
                if isinstance(constant, int)
                else "number"
                if isinstance(constant, float) and math.isfinite(constant)
                else "string"
                if isinstance(constant, str)
                else None
            )
            schema_types = (inferred,) if inferred is not None else ()
        else:
            schema_types = ()
        if (
            not schema_types
            or len(schema_types) != len(set(schema_types))
            or any(item not in _SCHEMA_TYPES for item in schema_types)
        ):
            raise ValueError("output schema type must be one of the supported JSON types")
        if root and schema_types != ("object",):
            raise ValueError("execution output schema must require an object")
        structural_keys = set(node) - _SCHEMA_METADATA_KEYS - {"type", "const", "enum"}
        if len(schema_types) > 1 and any(item in {"object", "array"} for item in schema_types):
            raise ValueError("object and array schema types cannot be unions")

        if "const" in node and not any(value_matches_type(node["const"], item) for item in schema_types):
            raise ValueError("output schema const is incompatible with its declared type")
        if "enum" in node:
            enum = node["enum"]
            if not isinstance(enum, list | tuple) or not enum:
                raise ValueError("output schema enum must be a non-empty array")
            if any(
                _json_schema_instance_equal(left, right)
                for index, left in enumerate(enum)
                for right in enum[index + 1 :]
            ) or any(not any(value_matches_type(value, item) for item in schema_types) for value in enum):
                raise ValueError("output schema enum must contain unique values compatible with its declared type")

        if "string" in schema_types:
            allowed = {"minLength", "maxLength", "pattern"}
            minimum = bounded_integer(node.get("minLength", 0), label="minLength", maximum=MAX_STRUCTURED_OUTPUT_BYTES)
            maximum = bounded_integer(
                node.get("maxLength", MAX_STRUCTURED_OUTPUT_BYTES),
                label="maxLength",
                maximum=MAX_STRUCTURED_OUTPUT_BYTES,
            )
            if minimum > maximum:
                raise ValueError("output schema string bounds are inverted")
            if "pattern" in node:
                pattern = node["pattern"]
                if not isinstance(pattern, str) or len(pattern.encode("utf-8")) > MAX_OUTPUT_PROPERTY_NAME_BYTES:
                    raise ValueError("output schema pattern must be a bounded string")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError("output schema pattern is invalid") from exc
            structural_keys -= allowed

        if len(schema_types) > 1:
            if structural_keys:
                raise ValueError("union schema constraints do not apply to a declared type")
            return
        schema_type = schema_types[0]

        if schema_type == "object":
            minimum = bounded_integer(
                node.get("minProperties", 0), label="minProperties", maximum=MAX_OUTPUT_OBJECT_PROPERTIES
            )
            maximum = bounded_integer(
                node.get("maxProperties", MAX_OUTPUT_OBJECT_PROPERTIES),
                label="maxProperties",
                maximum=MAX_OUTPUT_OBJECT_PROPERTIES,
            )
            if minimum > maximum:
                raise ValueError("output schema object bounds are inverted")
            properties = node.get("properties")
            required = node.get("required")
            additional = node.get("additionalProperties")
            property_names = node.get("propertyNames")
            if isinstance(properties, Mapping):
                if additional is not False or property_names is not None:
                    raise ValueError("named object schemas must be closed")
                constant_map = _is_closed_constant_map(node, properties)
                if not constant_map:
                    if not isinstance(required, list | tuple) or any(not isinstance(name, str) for name in required):
                        raise ValueError("named object schema required must contain property names")
                    required_names = set(required)
                    if not required_names <= set(properties):
                        raise ValueError("record schemas required names must be declared properties")
                    optional = set(properties) - required_names
                    version_property = properties.get("schema_version")
                    is_optional_plugin_capsule = (
                        optional == {"plugin_requirements"}
                        and isinstance(version_property, Mapping)
                        and version_property.get("const") == 3
                        and "local_image_paths" in properties
                    )
                    if optional and not is_optional_plugin_capsule:
                        raise ValueError("record schemas must require every declared property")
                    if len(required) != len(set(required)):
                        raise ValueError("record schema required names must be unique")
                if constant_map:
                    if minimum > len(properties) or maximum > len(properties):
                        raise ValueError("constant-map schema bounds exceed its supported keys")
                elif not minimum <= len(properties) <= maximum:
                    raise ValueError("record schema properties violate declared bounds")
            elif (
                properties is None
                and required is None
                and isinstance(property_names, Mapping)
                and isinstance(additional, Mapping)
            ):
                if "minProperties" not in node or "maxProperties" not in node:
                    raise ValueError("semantic map schemas require explicit property-count bounds")
                validate_node(property_names, depth=depth + 1, root=False)
                property_name_type = property_names.get("type")
                if property_name_type != "string":
                    raise ValueError("semantic map propertyNames must declare a string schema")
                validate_node(additional, depth=depth + 1, root=False)
                return
            else:
                raise ValueError("object schema must be a closed record or bounded semantic map")
            if structural_keys - {"properties", "required", "additionalProperties", "minProperties", "maxProperties"}:
                raise ValueError("object schema contains inapplicable constraints")
            properties_seen += len(properties)
            if properties_seen > MAX_OUTPUT_SCHEMA_PROPERTIES:
                raise ValueError("output schema exceeds the total property limit")
            for name, child in properties.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name.encode("utf-8")) > MAX_OUTPUT_PROPERTY_NAME_BYTES
                    or "\x00" in name
                ):
                    raise ValueError("object schema property names must be bounded non-empty strings")
                validate_node(child, depth=depth + 1, root=False)
            return

        if schema_type == "array":
            if "items" not in node:
                raise ValueError("array schemas require an explicit item schema")
            minimum = bounded_integer(node.get("minItems", 0), label="minItems", maximum=MAX_OUTPUT_ARRAY_ITEMS)
            maximum = bounded_integer(
                node.get("maxItems", MAX_OUTPUT_ARRAY_ITEMS), label="maxItems", maximum=MAX_OUTPUT_ARRAY_ITEMS
            )
            if minimum > maximum or ("uniqueItems" in node and not isinstance(node["uniqueItems"], bool)):
                raise ValueError("output schema array bounds or uniqueness are invalid")
            if structural_keys - {"items", "minItems", "maxItems", "uniqueItems"}:
                raise ValueError("array schema contains inapplicable constraints")
            validate_node(node["items"], depth=depth + 1, root=False)
            return

        if schema_type in {"number", "integer"}:
            numeric_keys = {"multipleOf", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
            for key in numeric_keys & set(node):
                if not _is_finite_json_number(node[key]):
                    raise ValueError(f"output schema {key} must be a finite JSON number")
            multiple = node.get("multipleOf")
            if multiple is not None and float(cast(int | float, multiple)) <= 0:
                raise ValueError("output schema multipleOf must be positive")
            minimum = node.get("minimum")
            maximum = node.get("maximum")
            exclusive_minimum = node.get("exclusiveMinimum")
            exclusive_maximum = node.get("exclusiveMaximum")
            lower = exclusive_minimum if exclusive_minimum is not None else minimum
            upper = exclusive_maximum if exclusive_maximum is not None else maximum
            if (
                lower is not None
                and upper is not None
                and float(cast(int | float, lower)) > float(cast(int | float, upper))
            ):
                raise ValueError("output schema numeric bounds are inverted")
            if structural_keys - numeric_keys:
                raise ValueError(f"{schema_type} schema contains inapplicable constraints")
            return

        if structural_keys:
            raise ValueError(f"{schema_type} schema contains inapplicable constraints")

    try:
        validate_node(schema, depth=1, root=True)
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError("output schema is not a valid JSON schema mapping") from exc


def validate_structured_output(value: object, schema: Mapping[str, object]) -> None:
    """Validate decoded JSON with the same bounded schema authority used for acceptance."""

    validate_output_schema(schema)
    try:
        encoded = json.dumps(
            thaw_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("structured output is not interoperable JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_STRUCTURED_OUTPUT_BYTES:
        raise ValueError("structured output exceeds the byte limit")

    properties_seen = 0

    def type_matches(item: object, schema_type: str) -> bool:
        if schema_type == "null":
            return item is None
        if schema_type == "boolean":
            return isinstance(item, bool)
        if schema_type == "integer":
            return isinstance(item, int) and not isinstance(item, bool)
        if schema_type == "number":
            return _is_finite_json_number(item)
        if schema_type == "string":
            return isinstance(item, str)
        if schema_type == "array":
            return isinstance(item, list | tuple)
        return isinstance(item, Mapping)

    def validate(item: object, node: Mapping[str, object], *, path: str, depth: int) -> None:
        nonlocal properties_seen
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise ValueError("structured output exceeds the depth limit")
        branches = node.get("oneOf")
        if isinstance(branches, Sequence) and not isinstance(branches, str | bytes | bytearray):
            baseline = properties_seen
            matches: list[int] = []
            matched_properties = baseline
            for index, branch in enumerate(branches):
                properties_seen = baseline
                try:
                    validate(item, cast(Mapping[str, object], branch), path=path, depth=depth + 1)
                except ValueError:
                    continue
                matches.append(index)
                matched_properties = properties_seen
            if len(matches) != 1:
                properties_seen = baseline
                raise ValueError(f"structured output field {path!r} must match exactly one oneOf branch")
            properties_seen = matched_properties
            return
        raw_type = node.get("type")
        if isinstance(raw_type, str):
            schema_types = (raw_type,)
        elif isinstance(raw_type, Sequence) and not isinstance(raw_type, str | bytes | bytearray):
            schema_types = tuple(cast(Sequence[str], raw_type))
        else:
            constant = node.get("const")
            inferred = (
                "null"
                if constant is None
                else "boolean"
                if isinstance(constant, bool)
                else "integer"
                if isinstance(constant, int)
                else "number"
                if isinstance(constant, float) and math.isfinite(constant)
                else "string"
                if isinstance(constant, str)
                else ""
            )
            schema_types = (inferred,)
        matching_types = tuple(schema_type for schema_type in schema_types if type_matches(item, schema_type))
        if not matching_types:
            raise ValueError(f"structured output field {path!r} has the wrong JSON type")
        if "const" in node and not _json_schema_instance_equal(item, node["const"]):
            raise ValueError(f"structured output field {path!r} violates const")
        if "enum" in node and not any(
            _json_schema_instance_equal(item, choice) for choice in cast(Sequence[object], node["enum"])
        ):
            raise ValueError(f"structured output field {path!r} is not in enum")
        if item is None:
            return
        schema_type = matching_types[0]
        if schema_type == "string":
            text = cast(str, item)
            if not node.get("minLength", 0) <= len(text) <= node.get("maxLength", MAX_STRUCTURED_OUTPUT_BYTES):
                raise ValueError(f"structured output field {path!r} violates string bounds")
            pattern = node.get("pattern")
            if isinstance(pattern, str) and re.search(pattern, text) is None:
                raise ValueError(f"structured output field {path!r} violates pattern")
            return
        if schema_type == "array":
            items = cast(Sequence[object], item)
            if not node.get("minItems", 0) <= len(items) <= node.get("maxItems", MAX_OUTPUT_ARRAY_ITEMS):
                raise ValueError(f"structured output field {path!r} violates array bounds")
            if node.get("uniqueItems", False) and any(
                _json_schema_instance_equal(left, right)
                for index, left in enumerate(items)
                for right in items[index + 1 :]
            ):
                raise ValueError(f"structured output field {path!r} violates uniqueItems")
            child_schema = cast(Mapping[str, object], node["items"])
            for index, child in enumerate(items):
                validate(child, child_schema, path=f"{path}[{index}]", depth=depth + 1)
            return
        if schema_type in {"number", "integer"}:
            number = cast(int | float, item)
            minimum = node.get("minimum")
            maximum = node.get("maximum")
            exclusive_minimum = node.get("exclusiveMinimum")
            exclusive_maximum = node.get("exclusiveMaximum")
            if minimum is not None and number < cast(int | float, minimum):
                raise ValueError(f"structured output field {path!r} violates minimum")
            if maximum is not None and number > cast(int | float, maximum):
                raise ValueError(f"structured output field {path!r} violates maximum")
            if exclusive_minimum is not None and number <= cast(int | float, exclusive_minimum):
                raise ValueError(f"structured output field {path!r} violates exclusiveMinimum")
            if exclusive_maximum is not None and number >= cast(int | float, exclusive_maximum):
                raise ValueError(f"structured output field {path!r} violates exclusiveMaximum")
            multiple = node.get("multipleOf")
            if multiple is not None:
                quotient = number / cast(int | float, multiple)
                if not math.isclose(float(quotient), round(float(quotient)), rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError(f"structured output field {path!r} violates multipleOf")
            return
        if schema_type != "object":
            return
        mapping = cast(Mapping[str, object], item)
        if any(not isinstance(key, str) for key in mapping):
            raise ValueError(f"structured output field {path!r} has a non-string object key")
        if not node.get("minProperties", 0) <= len(mapping) <= node.get("maxProperties", MAX_OUTPUT_OBJECT_PROPERTIES):
            raise ValueError(f"structured output field {path!r} violates object bounds")
        properties_seen += len(mapping)
        if properties_seen > MAX_OUTPUT_SCHEMA_PROPERTIES:
            raise ValueError("structured output exceeds the total property limit")
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            expected = set(properties)
            if _is_closed_constant_map(node, properties):
                if not set(mapping) <= expected:
                    raise ValueError(f"structured output field {path!r} contains an unsupported map key")
            elif set(mapping) != expected:
                raise ValueError(f"structured output field {path!r} does not match the closed record")
            for key in mapping:
                validate(
                    mapping[key],
                    cast(Mapping[str, object], properties[key]),
                    path=f"{path}.{key}",
                    depth=depth + 1,
                )
            return
        name_schema = cast(Mapping[str, object], node["propertyNames"])
        value_schema = cast(Mapping[str, object], node["additionalProperties"])
        for key, child in mapping.items():
            validate(key, name_schema, path=f"{path} property name", depth=depth + 1)
            validate(child, value_schema, path=f"{path}.{key}", depth=depth + 1)

    try:
        validate(value, schema, path="output", depth=1)
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError("structured output does not match the accepted schema") from exc


def _relative_paths(values: tuple[str, ...], *, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        path = Path(value)
        if (
            not value
            or value == "."
            or "\x00" in value
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != value
            or value in seen
        ):
            raise ValueError(f"{label} must contain unique repository-relative paths")
        seen.add(value)


MAX_LOCAL_IMAGE_COUNT = 8
MAX_LOCAL_IMAGE_PATH_BYTES = 4_096
MAX_LOCAL_IMAGE_BYTES = 20 * 1_048_576
_LOCAL_IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".bmp": (b"BM",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".jpeg": (b"\xff\xd8\xff",),
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".webp": (b"RIFF",),
}


def validate_local_image_inputs(paths: tuple[str, ...] | list[str], *, workspace: Path) -> tuple[LocalImageInput, ...]:
    """Validate image topology and type without retaining image bytes.

    Only a bounded signature prefix is read through a no-follow descriptor.
    The returned values contain paths only; callers pass those values to the
    official SDK input constructors, which own the later image read.
    """

    if not isinstance(paths, tuple | list):
        raise ValueError("local image paths must be an array")
    values = tuple(paths)
    if not 1 <= len(values) <= MAX_LOCAL_IMAGE_COUNT or any(not isinstance(item, str) for item in values):
        raise ValueError("local image paths must contain one to eight strings")
    _relative_paths(values, label="local image paths")
    if any("\x00" in value or len(os.fsencode(value)) > MAX_LOCAL_IMAGE_PATH_BYTES for value in values):
        raise ValueError("local image path exceeds its byte limit")
    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("local image workspace is unavailable") from exc
    if not root.is_dir():
        raise ValueError("local image workspace is not a directory")

    validated: list[LocalImageInput] = []
    for relative in values:
        relative_path = Path(relative)
        candidate = root.joinpath(*relative_path.parts)
        current = root
        try:
            for part in relative_path.parts:
                current /= part
                metadata = os.lstat(current)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("local image path contains a symlink")
            metadata = os.lstat(candidate)
        except FileNotFoundError as exc:
            raise ValueError("local image path is missing") from exc
        except OSError as exc:
            raise ValueError("local image path cannot be inspected") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("local image path is not a regular file")
        if metadata.st_nlink != 1:
            raise ValueError("local image path must have one link")
        if metadata.st_size > MAX_LOCAL_IMAGE_BYTES:
            raise ValueError("local image file exceeds its byte limit")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("local image path cannot be resolved") from exc
        if resolved != candidate or not resolved.is_relative_to(root):
            raise ValueError("local image path escapes the workspace")
        suffix = candidate.suffix.casefold()
        signatures = _LOCAL_IMAGE_SIGNATURES.get(suffix)
        if signatures is None:
            raise ValueError("local image file type is unsupported")
        descriptor = -1
        try:
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise ValueError("local image file changed during validation")
            prefix = os.read(descriptor, 12)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("local image file cannot be read") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if suffix == ".webp":
            valid_signature = len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
        else:
            valid_signature = any(prefix.startswith(signature) for signature in signatures)
        del prefix
        if not valid_signature:
            raise ValueError("local image file signature is unsupported")
        validated.append(LocalImageInput(relative))
    return tuple(validated)


@dataclass(frozen=True, slots=True)
class ExecutionCapsule:
    capsule_version: int
    run_id: RunId
    milestone_id: MilestoneId
    repository_root: Path
    workspace_mode: WorkspaceMode
    workspace_path: Path
    branch: str
    base_sha: str
    lane: str
    mutable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    validation: ValidationSpec
    model: str
    reasoning_effort: ReasoningEffort
    prompt: str
    output_schema: JsonObject
    permission_mode: NativePermissionMode = NativePermissionMode.INHERIT_NATIVE
    acceptance_modes: tuple[AcceptanceMode, ...] = (AcceptanceMode.OBJECTIVE,)
    plugin_requirements: tuple[JsonObject, ...] = ()
    local_image_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.capsule_version, bool) or not isinstance(self.capsule_version, int):
            raise ValueError("execution capsule version must be an integer")
        if self.capsule_version not in {2, 3}:
            raise ValueError("unsupported execution capsule version")
        try:
            repository_root = self.repository_root.resolve(strict=False)
            workspace_path = self.workspace_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("repository and workspace paths must have a canonical physical identity") from exc
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "workspace_path", workspace_path)
        if _LANE_PATTERN.fullmatch(self.lane) is None:
            raise ValueError("workspace lane must be a lowercase semantic slug")
        if not self.branch or any(character.isspace() for character in self.branch):
            raise ValueError("execution branch must be explicit and whitespace-free")
        if self.workspace_mode is WorkspaceMode.MANAGED_WORKTREE and self.branch != f"agent/{self.lane}":
            raise ValueError("managed worktree branch must match agent/<lane>")
        if len(self.base_sha) != 40 or re.fullmatch(r"[0-9a-f]{40}", self.base_sha) is None:
            raise ValueError("base SHA must be a resolved lowercase 40-character Git SHA")
        _relative_paths(self.mutable_paths, label="mutable paths")
        _relative_paths(self.protected_paths, label="protected paths")
        for mutable in map(Path, self.mutable_paths):
            for protected in map(Path, self.protected_paths):
                if mutable == protected or mutable in protected.parents or protected in mutable.parents:
                    raise ValueError("mutable and protected paths must not overlap")
        if not self.model.strip() or not self.prompt.strip():
            raise ValueError("model and prompt must be explicit")
        try:
            # A frozen dataclass alone is not sufficient here: callers can
            # retain and mutate nested dict/list aliases after construction.
            # Own the entire schema tree before it can reach a digest, SDK,
            # ledger, or artifact.
            canonical_schema = freeze_json(self.output_schema)
        except (TypeError, RecursionError) as exc:
            raise ValueError("execution output schema is not a valid JSON object") from exc
        if not isinstance(canonical_schema, Mapping):  # pragma: no cover - predicate already enforces object root
            raise ValueError("execution output schema must be an object")
        validate_output_schema(canonical_schema)
        object.__setattr__(self, "output_schema", canonical_schema)
        modes = tuple(
            mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in self.acceptance_modes
        )
        if not modes or len(modes) != len(set(modes)):
            raise ValueError("execution capsule acceptance modes must be non-empty and unique")
        object.__setattr__(self, "acceptance_modes", modes)
        requirements = tuple(self.plugin_requirements)
        if any(not isinstance(item, Mapping) for item in requirements):
            raise ValueError("execution plugin requirements must be objects")
        object.__setattr__(self, "plugin_requirements", tuple(dict(item) for item in requirements))
        image_paths = tuple(self.local_image_paths)
        if any(not isinstance(item, str) for item in image_paths):
            raise ValueError("execution local image paths must be strings")
        if self.capsule_version == 2 and image_paths:
            raise ValueError("execution capsule version 2 cannot carry local images")
        if self.capsule_version == 3 and not image_paths:
            raise ValueError("execution capsule version 3 requires local images")
        _relative_paths(image_paths, label="execution local image paths")
        object.__setattr__(self, "local_image_paths", image_paths)


class ProgramId(_ValidatedIdentifier):
    """Stable identifier for one registered program graph."""

    label = "program id"


class ProgramEventKind(str, Enum):
    """Durable events that may wake one ephemeral program controller."""

    IMPLEMENTATION_COMPLETED = "implementation_completed"
    REVIEW_COMPLETED = "review_completed"
    INTEGRATION_COMPLETED = "integration_completed"
    CONTROLLER_ATTENTION = "controller_attention"
    CHECKPOINT = "checkpoint"


class ProgramControllerActionKind(str, Enum):
    """Closed effects a program-controller generation may authorize."""

    START_READY_MILESTONES = "start_ready_milestones"
    START_REVIEWS = "start_reviews"
    REQUEST_REPAIR = "request_repair"
    PROMOTE_CANDIDATE = "promote_candidate"
    INTEGRATE_CANDIDATE = "integrate_candidate"
    REQUIRE_REPLAN = "require_replan"
    REQUIRE_HUMAN_ATTENTION = "require_human_attention"
    ACKNOWLEDGE_ONLY = "acknowledge_only"


class ProgramState(str, Enum):
    """Dynamic program projection kept separately from static plan intent."""

    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProgramNodeSpec:
    """One executable graph node with immutable ownership and dependencies."""

    milestone_id: MilestoneId
    capsule: ExecutionCapsule
    dependencies: tuple[MilestoneId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.milestone_id, MilestoneId):
            object.__setattr__(self, "milestone_id", MilestoneId(self.milestone_id))
        if not isinstance(self.capsule, ExecutionCapsule):
            raise ValueError("program node capsule must be an ExecutionCapsule")
        if self.capsule.milestone_id != self.milestone_id:
            raise ValueError("program node milestone does not match its capsule")
        dependencies = tuple(item if isinstance(item, MilestoneId) else MilestoneId(item) for item in self.dependencies)
        if self.milestone_id in dependencies or len(dependencies) != len(set(dependencies)):
            raise ValueError("program node dependencies must be unique and acyclic at the node boundary")
        object.__setattr__(self, "dependencies", dependencies)

    @property
    def mutable_surfaces(self) -> tuple[str, ...]:
        return self.capsule.mutable_paths

    @property
    def workspace_path(self) -> Path:
        return self.capsule.workspace_path


@dataclass(frozen=True, slots=True)
class ProgramGraph:
    """Closed executable graph compiled from one canonical plan revision."""

    program_id: ProgramId
    plan_path: Path
    plan_revision_sha256: str
    nodes: tuple[ProgramNodeSpec, ...]
    trunk_head: str
    integration_strategy: str = "merge"

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, ProgramId):
            object.__setattr__(self, "program_id", ProgramId(self.program_id))
        plan_path = Path(self.plan_path)
        if not plan_path.is_absolute() or ".." in plan_path.parts:
            raise ValueError("program plan path must be an absolute canonical path")
        object.__setattr__(self, "plan_path", plan_path)
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_revision_sha256) is None:
            raise ValueError("program plan revision digest is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.trunk_head) is None:
            raise ValueError("program trunk HEAD is invalid")
        if self.integration_strategy not in {"merge", "fast_forward", "cherry_pick"}:
            raise ValueError("program integration strategy is unsupported")
        nodes = tuple(self.nodes)
        if not nodes or len(nodes) > 128:
            raise ValueError("program graph must contain one to 128 nodes")
        ids = tuple(node.milestone_id for node in nodes)
        if len(set(ids)) != len(ids):
            raise ValueError("program graph milestone ids must be unique")
        known = set(ids)
        if any(dependency not in known for node in nodes for dependency in node.dependencies):
            raise ValueError("program graph dependency is not declared")
        graph = {node.milestone_id: set(node.dependencies) for node in nodes}
        visiting: set[MilestoneId] = set()
        visited: set[MilestoneId] = set()

        def visit(node_id: MilestoneId) -> None:
            if node_id in visiting:
                raise ValueError("program graph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in graph[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        owned_paths: dict[str, MilestoneId] = {}
        for node in nodes:
            for surface in node.mutable_surfaces:
                owner = owned_paths.get(surface)
                if owner is not None:
                    raise ValueError(
                        f"program mutable surface {surface!r} is owned by both {owner} and {node.milestone_id}"
                    )
                owned_paths[surface] = node.milestone_id
        object.__setattr__(self, "nodes", nodes)

    def node(self, milestone_id: MilestoneId | str) -> ProgramNodeSpec:
        target = milestone_id if isinstance(milestone_id, MilestoneId) else MilestoneId(milestone_id)
        for node in self.nodes:
            if node.milestone_id == target:
                return node
        raise KeyError(target)

    @property
    def ready_roots(self) -> tuple[ProgramNodeSpec, ...]:
        return tuple(node for node in self.nodes if not node.dependencies)


@dataclass(frozen=True, slots=True)
class ProgramNodeStatus:
    """Durable dynamic projection for one program node."""

    milestone_id: MilestoneId
    state: str
    dependencies: tuple[MilestoneId, ...] = ()
    candidate_sha: str | None = None
    review_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    integrated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.milestone_id, MilestoneId):
            object.__setattr__(self, "milestone_id", MilestoneId(self.milestone_id))
        if not isinstance(self.state, str) or not self.state.strip() or len(self.state) > 64:
            raise ValueError("program node state is invalid")
        dependencies = tuple(item if isinstance(item, MilestoneId) else MilestoneId(item) for item in self.dependencies)
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("program node dependencies are not unique")
        object.__setattr__(self, "dependencies", dependencies)
        for label, values in (("review ids", self.review_ids), ("finding ids", self.finding_ids)):
            checked = tuple(values)
            if any(not isinstance(value, str) or not value.strip() for value in checked):
                raise ValueError(f"program {label} are invalid")
            object.__setattr__(self, label.replace(" ", "_"), tuple(sorted(set(checked))))
        if self.candidate_sha is not None and re.fullmatch(r"[0-9a-f]{40}", self.candidate_sha) is None:
            raise ValueError("program candidate commit is invalid")
        if not isinstance(self.integrated, bool):
            raise ValueError("program integration state is invalid")

    @property
    def ready(self) -> bool:
        return self.state == WorkflowState.PLANNED.value and not self.integrated


@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """Closed read projection of one registered program."""

    program_id: ProgramId
    state: ProgramState
    revision: int
    plan_digest: str
    trunk_head: str
    nodes: tuple[ProgramNodeStatus, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, ProgramId):
            object.__setattr__(self, "program_id", ProgramId(self.program_id))
        if not isinstance(self.state, ProgramState):
            object.__setattr__(self, "state", ProgramState(self.state))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("program revision is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_digest) is None:
            raise ValueError("program plan digest is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.trunk_head) is None:
            raise ValueError("program trunk HEAD is invalid")
        nodes = tuple(self.nodes)
        if not nodes or len(nodes) > 128 or len({node.milestone_id for node in nodes}) != len(nodes):
            raise ValueError("program node projection is invalid")
        object.__setattr__(self, "nodes", nodes)

    @property
    def ready_milestones(self) -> tuple[MilestoneId, ...]:
        integrated = {node.milestone_id for node in self.nodes if node.integrated}
        return tuple(
            node.milestone_id
            for node in self.nodes
            if node.ready and all(dependency in integrated for dependency in node.dependencies)
        )


@dataclass(frozen=True, slots=True)
class ProgramControllerDecisionStatus:
    """Program subject projection over the shared controller decision rows."""

    decision_id: ControllerDecisionId
    program_id: ProgramId
    event_kind: ProgramEventKind
    event_key: str
    state: ControllerDecisionState
    revision: int
    current_generation: Generation
    generation_budget: int
    generation_used: int
    claimant_kind: ControllerClaimantKind | None
    claimant_id: str | None
    claim_expires_at: str | None
    action_id: str | None
    action_sha256: str | None
    deadline: str
    payload: JsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.program_id, ProgramId):
            object.__setattr__(self, "program_id", ProgramId(self.program_id))
        if not isinstance(self.event_kind, ProgramEventKind):
            object.__setattr__(self, "event_kind", ProgramEventKind(self.event_kind))
        _required_text(self.event_key, label="program event key", limit=256)
        if not isinstance(self.state, ControllerDecisionState):
            object.__setattr__(self, "state", ControllerDecisionState(self.state))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("program decision revision is invalid")
        if not isinstance(self.current_generation, Generation):
            object.__setattr__(self, "current_generation", Generation(self.current_generation))
        if isinstance(self.generation_budget, bool) or not 1 <= self.generation_budget <= 2:
            raise ValueError("program generation budget is invalid")
        if isinstance(self.generation_used, bool) or not 0 <= self.generation_used <= self.generation_budget:
            raise ValueError("program generation usage is invalid")
        if self.claimant_kind is not None and not isinstance(self.claimant_kind, ControllerClaimantKind):
            object.__setattr__(self, "claimant_kind", ControllerClaimantKind(self.claimant_kind))
        if self.claimant_id is not None:
            _required_text(self.claimant_id, label="program claimant id", limit=256)
        _required_text(self.deadline, label="program decision deadline", limit=64)
        if self.action_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.action_sha256) is None:
            raise ValueError("program action digest is invalid")
        object.__setattr__(self, "payload", _owned_json_object(self.payload, label="program decision payload"))


def canonicalize_execution_capsule(capsule: ExecutionCapsule) -> ExecutionCapsule:
    """Revalidate and detach a capsule at every public durability boundary."""

    if not isinstance(capsule, ExecutionCapsule):
        raise ValueError("execution plan requires an ExecutionCapsule")
    # Reconstruct every field rather than trusting frozen-instance internals:
    # a caller with access to ``object.__setattr__`` or a custom mapping must
    # not bypass the public validation boundary before any SQLite write.
    return ExecutionCapsule(
        capsule.capsule_version,
        capsule.run_id,
        capsule.milestone_id,
        capsule.repository_root,
        capsule.workspace_mode,
        capsule.workspace_path,
        capsule.branch,
        capsule.base_sha,
        capsule.lane,
        tuple(capsule.mutable_paths),
        tuple(capsule.protected_paths),
        capsule.validation,
        capsule.model,
        capsule.reasoning_effort,
        capsule.prompt,
        capsule.output_schema,
        capsule.permission_mode,
        tuple(capsule.acceptance_modes),
        tuple(capsule.plugin_requirements),
        tuple(capsule.local_image_paths),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseRecord:
    workspace_path: Path
    repository_root: Path
    mode: WorkspaceMode
    branch: str
    base_sha: str
    lane: str
    owner_run_id: RunId
    created_at: str


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    timed_out: bool
    duration_seconds: float
    error_code: ValidationFailureCode | None = None

    def __post_init__(self) -> None:
        if self.exit_code == 0 and self.error_code is not None:
            raise ValueError("successful validation cannot carry a failure code")
        if self.timed_out and self.error_code not in {
            None,
            ValidationFailureCode.TIMEOUT,
            ValidationFailureCode.INTEGRITY_FAILURE,
        }:
            raise ValueError("timed-out validation has an incompatible failure code")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    run_id: RunId
    milestone_id: MilestoneId
    workspace_path: Path
    capsule_path: Path
    capsule_sha256: str
    model: str
    reasoning_effort: ReasoningEffort
    status: ExecutionStatus
    checkpoint: ControllerCheckpoint
    thread_id: ThreadIdentity | None
    turn_id: str | None
    turn_output: JsonObject | None
    result: JsonObject | None
    validation: ValidationObservation | None
    protected_before_sha256: str
    protected_after_sha256: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionIntegrityRecord:
    run_id: RunId
    milestone_id: MilestoneId
    provenance: str
    native_profile_sha256: str | None
    native_compatibility_sha256: str | None
    effective_permission: NativePermissionAuthority | None
    effective_permission_sha256: str | None
    workspace_baseline_head_sha: str | None
    workspace_baseline: tuple[tuple[str, str], ...] | None
    workspace_baseline_sha256: str | None
    workspace_terminal_head_sha: str | None
    workspace_terminal: tuple[tuple[str, str], ...] | None
    workspace_terminal_sha256: str | None
    turn_started_at: str | None
    git_authority_before_sha256: str | None
    git_authority_after_sha256: str | None
    created_at: str
    updated_at: str
