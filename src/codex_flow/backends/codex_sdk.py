"""The only H1 production Codex transport: the published Python SDK.

This adapter intentionally imports and touches ``openai_codex`` only here.
Tests inject a small SDK-shaped fake, while production uses the package's
high-level ``Codex``/``Thread``/``TurnHandle`` surface.  There is no CLI or
direct app-server fallback.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

from ..contracts import ModelFacingControllerActionBundle, ModelFacingResult
from ..domain import (
    CONVERSATION_FRAGMENT_BYTES,
    CONVERSATION_MAX_ITEMS,
    CONVERSATION_MAX_TEXT_BYTES,
    CONVERSATION_MAX_TURNS,
    CONVERSATION_PAGE_BYTES,
    MAX_STRUCTURED_OUTPUT_BYTES,
    STEER_TEXT_MAX_BYTES,
    Capability,
    CapabilityObservation,
    CapabilityStatus,
    CodexFlowError,
    ControllerDecisionId,
    ConversationContent,
    ConversationContentKind,
    ConversationHistoryPage,
    ConversationHistoryRequest,
    ConversationHistoryStatus,
    ConversationMessageFragment,
    ConversationSpeaker,
    ConversationTurnSlice,
    Generation,
    JsonObject,
    LifecycleEvent,
    LocalImageInput,
    NativePermissionAuthority,
    NativePermissionMode,
    ReasoningEffort,
    Sandbox,
    Schema,
    SkillInput,
    TemporaryRateLimitAfterIdentity,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    TransientFailureAfterIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    UnknownSdkFailureAfterIdentity,
    UnknownSdkFailureBeforeIdentity,
    UnsupportedCapability,
    redact_conversation_text,
    redact_diagnostic_text,
    strict_json_loads,
    thaw_json,
    validate_output_schema,
    validate_structured_output,
)
from ..native_profile import NativeProfileProjection


class _SdkThread(Protocol):
    id: str

    def turn(self, input: object, **kwargs: Any) -> _SdkTurn: ...


class _SdkTurn(Protocol):
    id: str

    def stream(self) -> Iterable[Any]: ...

    def steer(self, input: object) -> object: ...

    def interrupt(self) -> object: ...


class _SdkNotification(Protocol):
    method: str
    payload: object


class _SdkClient(Protocol):
    metadata: Any

    def thread_start(self, **kwargs: Any) -> _SdkThread: ...

    def thread_resume(self, thread_id: str, **kwargs: Any) -> _SdkThread: ...

    def thread_archive(self, thread_id: str) -> object: ...

    def close(self) -> None: ...


class ThreadInspectionKind(str, Enum):
    """Closed outcomes from one read-only persisted-thread inspection."""

    TERMINAL_RESULT = "terminal_result"
    INTERRUPTED = "interrupted"
    ACTIVE_WRITER = "active_writer"
    IDLE_NO_RESULT = "idle_no_result"
    EMPTY_HISTORY = "empty_history"
    FAILED_TURN = "failed_turn"
    AMBIGUOUS = "ambiguous"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ThreadInspection:
    thread_id: str
    kind: ThreadInspectionKind
    turn_id: str | None = None
    raw_result: str | None = None
    detail: str | None = None
    retry_at: str | None = None


class ControllerThreadInspectionKind(str, Enum):
    """Closed outcomes for one controller recovery thread read."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ControllerThreadInspection:
    thread_id: str
    generation: int
    kind: ControllerThreadInspectionKind
    turn_id: str | None = None
    bundle: ModelFacingControllerActionBundle | None = None
    detail: str | None = None


def _sdk_thread_read(client: _SdkClient, thread_id: str) -> object:
    """Call the pinned SDK raw thread/read boundary exactly once."""

    # The public facade intentionally omits this low-level recovery method;
    # the stable client remains reachable as the SDK's explicit raw boundary.
    try:
        raw_client = client._client  # type: ignore[attr-defined]
    except AttributeError:
        raw_client = client
    try:
        reader = raw_client.thread_read
    except AttributeError as exc:
        raise UnsupportedCapability("installed SDK does not expose raw thread_read") from exc
    return reader(thread_id, include_turns=True)


def _history_token(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_history_token(value: str) -> dict[str, object]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = strict_json_loads(raw, max_bytes=2048)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("conversation page token is malformed") from exc
    if not isinstance(decoded, dict):
        raise ValueError("conversation page token is malformed")
    return decoded


def _history_snapshot_facts(
    request: ConversationHistoryRequest, *, digest: str, updated: str | None
) -> dict[str, object]:
    return {
        "subject_kind": request.subject_kind.value,
        "subject_id": request.subject_id,
        "thread_id": request.thread_id.id,
        "generation": request.generation,
        "attempt": request.attempt,
        "revision": request.revision,
        "digest": digest,
        "updated": updated,
    }


def _history_marker(kind: str, _value: object) -> tuple[ConversationContentKind, str]:
    marker = {
        "image": ConversationContentKind.IMAGE,
        "localImage": ConversationContentKind.IMAGE,
        "audio": ConversationContentKind.AUDIO,
        "localAudio": ConversationContentKind.AUDIO,
        "skill": ConversationContentKind.SKILL,
        "mention": ConversationContentKind.MENTION,
    }.get(kind)
    if marker is None:
        raise ValueError("unsupported conversation content shape")
    # Paths, URLs, and user-controlled skill/mention names never cross the
    # boundary.  The complete message retains only the typed presence fact.
    return marker, f"[{marker.value}]"


def _history_fragments(
    snapshot: object, thread_id: str
) -> tuple[tuple[ConversationMessageFragment, ...], dict[str, int], int, str, str | None]:
    missing = object()
    thread = _read(snapshot, "thread", missing)
    if thread is missing:
        raise ValueError("conversation snapshot is malformed")
    snapshot_thread_id = _read(thread, "id", thread_id)
    if snapshot_thread_id != thread_id:
        raise ValueError("conversation snapshot thread identity conflicts")
    turns = _read(thread, "turns", missing)
    if not isinstance(turns, list) or len(turns) > CONVERSATION_MAX_TURNS:
        raise OverflowError("conversation source exceeds turn limit")
    fragments: list[ConversationMessageFragment] = []
    turn_ordinals: dict[str, int] = {}
    seen_items: set[str] = set()
    total_items = 0
    redactions = 0
    total_text_bytes = 0
    for turn_index, turn in enumerate(turns):
        turn_id = _read(turn, "id", missing)
        if not isinstance(turn_id, str) or not turn_id or any(character.isspace() for character in turn_id):
            raise ValueError("conversation turn identity is malformed")
        if turn_id in turn_ordinals:
            raise ValueError("conversation turn identity is duplicated")
        turn_ordinals[turn_id] = turn_index
        view = _enum_value(_read(turn, "items_view", _read(turn, "itemsView", "full")))
        if view not in {None, "full"}:
            raise IncompleteConversationHistory("SDK returned a non-full turn item view")
        items = _read(turn, "items", missing)
        if not isinstance(items, list):
            raise ValueError("conversation turn items are malformed")
        for item in items:
            total_items += 1
            if total_items > CONVERSATION_MAX_ITEMS:
                raise OverflowError("conversation source exceeds item limit")
            root = _read(item, "root", item)
            item_type = _read(root, "type", None)
            if not isinstance(item_type, str):
                raise ValueError("conversation item type is malformed")
            if item_type not in {"agentMessage", "userMessage"}:
                continue
            item_id = _read(root, "id", None)
            if not isinstance(item_id, str) or not item_id or any(character.isspace() for character in item_id):
                raise ValueError("conversation item identity is malformed")
            if item_id in seen_items:
                raise ValueError("conversation item identity is duplicated")
            seen_items.add(item_id)
            if item_type == "agentMessage":
                text = _read(root, "text", None)
                if not isinstance(text, str):
                    raise ValueError("agent conversation message is malformed")
                content_values = ((ConversationContentKind.TEXT, text),)
                speaker = ConversationSpeaker.AGENT
            elif item_type == "userMessage":
                content = _read(root, "content", missing)
                if not isinstance(content, list):
                    raise ValueError("user conversation message is malformed")
                values: list[tuple[ConversationContentKind, str]] = []
                for value in content:
                    content_root = _read(value, "root", value)
                    content_type = _read(content_root, "type", None)
                    if content_type == "text":
                        text = _read(content_root, "text", None)
                        if not isinstance(text, str):
                            raise ValueError("user text content is malformed")
                        values.append((ConversationContentKind.TEXT, text))
                    elif isinstance(content_type, str):
                        values.append(_history_marker(content_type, content_root))
                    else:
                        raise ValueError("user content type is malformed")
                if not values:
                    values.append((ConversationContentKind.TEXT, ""))
                content_values = tuple(values)
                speaker = ConversationSpeaker.USER
            for content_kind, text in content_values:
                content_redacted = False
                if content_kind is ConversationContentKind.TEXT:
                    redacted_text, changed = redact_conversation_text(text)
                    content_redacted = changed
                    redactions += int(changed)
                    text = redacted_text
                    total_text_bytes += len(text.encode("utf-8"))
                    if total_text_bytes > CONVERSATION_MAX_TEXT_BYTES:
                        raise OverflowError("conversation source exceeds text limit")
                    pieces: list[str] = []
                    current: list[str] = []
                    current_bytes = 0
                    for character in text:
                        size = len(character.encode("utf-8"))
                        if current and current_bytes + size > CONVERSATION_FRAGMENT_BYTES:
                            pieces.append("".join(current))
                            current = []
                            current_bytes = 0
                        current.append(character)
                        current_bytes += size
                    pieces.append("".join(current)) if current or not pieces else None
                else:
                    pieces = [text]
                for piece in pieces:
                    fragments.append(
                        ConversationMessageFragment(
                            turn_id,
                            item_id,
                            speaker,
                            ConversationContent(content_kind, piece, content_redacted),
                            len(fragments),
                        )
                    )
    ordered_identity = [
        (item.turn_id, item.item_id, item.speaker.value, item.content.kind.value, item.content.text)
        for item in fragments
    ]
    digest = hashlib.sha256(
        json.dumps(ordered_identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    updated = _read(thread, "updated_at", _read(thread, "updatedAt", None))
    updated_text = str(updated) if updated is not None else None
    return tuple(fragments), turn_ordinals, redactions, digest, updated_text


class IncompleteConversationHistory(ValueError):
    """The official SDK returned a summary/partial conversation view."""


def _history_page(request: ConversationHistoryRequest, snapshot: object) -> ConversationHistoryPage:
    fragments, turn_ordinals, redactions, digest, updated = _history_fragments(snapshot, request.thread_id.id)
    total = len(fragments)
    offset = total
    if request.page_token is not None:
        token = _decode_history_token(request.page_token)
        expected = _history_snapshot_facts(request, digest=digest, updated=updated)
        if set(token) != set(expected) | {"offset"} or any(token.get(key) != value for key, value in expected.items()):
            return ConversationHistoryPage(
                request.subject_kind,
                request.subject_id,
                request.thread_id,
                ConversationHistoryStatus.STALE,
                None,
                reason="history changed; reload",
            )
        raw_offset = token.get("offset")
        if isinstance(raw_offset, bool) or not isinstance(raw_offset, int) or not 0 <= raw_offset <= total:
            raise ValueError("conversation page token offset is invalid")
        offset = raw_offset
    start = max(0, offset - request.page_fragments)
    while start < offset:
        selected = fragments[start:offset]
        grouped: dict[tuple[str, int], list[ConversationMessageFragment]] = {}
        for fragment in selected:
            grouped.setdefault((fragment.turn_id, turn_ordinals[fragment.turn_id]), []).append(fragment)
        turns = tuple(
            ConversationTurnSlice(turn_id, ordinal, tuple(messages)) for (turn_id, ordinal), messages in grouped.items()
        )
        page = ConversationHistoryPage(
            request.subject_kind,
            request.subject_id,
            request.thread_id,
            ConversationHistoryStatus.AVAILABLE,
            _history_token(_history_snapshot_facts(request, digest=digest, updated=updated)),
            turns,
            _history_token(
                {
                    **_history_snapshot_facts(request, digest=digest, updated=updated),
                    "offset": start,
                }
            )
            if start > 0
            else None,
            redactions,
        )
        if (
            len(json.dumps(page.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            <= CONVERSATION_PAGE_BYTES
            or start == offset - 1
        ):
            return page
        start += 1
    return ConversationHistoryPage(
        request.subject_kind,
        request.subject_id,
        request.thread_id,
        ConversationHistoryStatus.AVAILABLE,
        _history_token(
            {
                **_history_snapshot_facts(request, digest=digest, updated=updated),
                "offset": offset,
            }
        ),
        (),
        None,
        redactions,
    )


def _active_writer_error(value: BaseException) -> bool:
    """Recognize only the exact structured active-writer RPC error."""

    if type(value).__name__ != "InvalidRequestError":
        return False
    try:
        code = value.code  # type: ignore[attr-defined]
        message = value.message  # type: ignore[attr-defined]
    except AttributeError:
        return False
    return code == -32600 and message == "the thread already has an active writer"


def _terminal_from_thread_snapshot(snapshot: object, thread_id: str) -> ThreadInspection:
    """Extract only a complete typed terminal envelope from bounded turns."""

    thread = _read(snapshot, "thread", snapshot)
    snapshot_thread_id = _read(thread, "id", None)
    if snapshot_thread_id is not None and snapshot_thread_id != thread_id:
        return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="thread identity is conflicting")
    missing = object()
    turns = _read(thread, "turns", missing)
    if turns is missing or not isinstance(turns, list) or len(turns) > 64:
        return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="turn snapshot is malformed")
    if not turns:
        return ThreadInspection(thread_id, ThreadInspectionKind.EMPTY_HISTORY)
    # The SDK returns turns in chronological order.  Keep only the final turn
    # as a possible terminal authority: an older valid envelope must never be
    # ingested when a later turn is active, malformed, summary-only, or
    # otherwise non-terminal.
    latest_status: str | None = None
    latest_turn_id: str | None = None
    latest_candidates: list[tuple[str, str]] = []
    latest_invalid_agent_message = False
    latest_retry_at: str | None = None
    for index, turn in enumerate(turns):
        turn_id = _read(turn, "id", missing)
        if not isinstance(turn_id, str) or not turn_id or any(character.isspace() for character in turn_id):
            return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="turn identity is malformed")
        status = _enum_value(_read(turn, "status", missing))
        if status is missing or status not in {
            "completed",
            "failed",
            "interrupted",
            "inProgress",
            "in_progress",
            "active",
            "running",
        }:
            return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="turn status is malformed")
        candidates: list[tuple[str, str]] = []
        invalid_agent_message = False
        items = _read(turn, "items", missing)
        if items is missing or not isinstance(items, list) or len(items) > 4096:
            return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="turn items are unbounded")
        for item in items:
            root = _read(item, "root", missing)
            if root is missing:
                return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="thread item is malformed")
            item_type = _read(root, "type")
            text = _read(root, "text")
            if item_type is None:
                return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="thread item type is missing")
            if item_type not in {"agentMessage", "agent_message"}:
                continue
            if not isinstance(text, str):
                invalid_agent_message = True
                continue
            if len(text.encode("utf-8")) > 65_536:
                return ThreadInspection(
                    thread_id, ThreadInspectionKind.MALFORMED, detail="terminal envelope is oversized"
                )
            try:
                ModelFacingResult.from_agent_message(text)
            except (TypeError, ValueError):
                invalid_agent_message = True
                continue
            if status == "completed":
                candidates.append((text, turn_id))
        if index == len(turns) - 1:
            latest_status = status
            latest_turn_id = turn_id
            latest_candidates = candidates
            latest_invalid_agent_message = invalid_agent_message
            latest_retry_at = _temporary_usage_limit_retry_at(_read(turn, "error"))

    if latest_status in {"inProgress", "in_progress", "active", "running"}:
        # A successful read does not prove ownership.  Only the exact RPC
        # active-writer error is authoritative; status alone is ambiguous.
        return ThreadInspection(
            thread_id, ThreadInspectionKind.AMBIGUOUS, detail="thread status is active without writer proof"
        )
    if latest_invalid_agent_message:
        return ThreadInspection(thread_id, ThreadInspectionKind.MALFORMED, detail="latest agent message is invalid")
    if latest_status != "completed":
        if latest_status == "interrupted":
            return ThreadInspection(
                thread_id,
                ThreadInspectionKind.INTERRUPTED,
                turn_id=latest_turn_id,
                detail="SDK turn ended with interrupted terminal evidence",
            )
        if latest_status == "failed":
            return ThreadInspection(
                thread_id,
                ThreadInspectionKind.FAILED_TURN,
                turn_id=latest_turn_id,
                detail="latest SDK turn failed",
                retry_at=latest_retry_at,
            )
        return ThreadInspection(thread_id, ThreadInspectionKind.AMBIGUOUS, detail="latest turn is not completed")
    if len(latest_candidates) > 1:
        return ThreadInspection(thread_id, ThreadInspectionKind.AMBIGUOUS, detail="conflicting terminal results")
    if latest_candidates:
        text, turn_id = latest_candidates[0]
        return ThreadInspection(thread_id, ThreadInspectionKind.TERMINAL_RESULT, turn_id or None, text)
    return ThreadInspection(thread_id, ThreadInspectionKind.IDLE_NO_RESULT)


def _controller_from_thread_snapshot(
    snapshot: object,
    thread_id: str,
    decision_id: ControllerDecisionId,
    generation: Generation,
    expected_revision: int | None = None,
) -> ControllerThreadInspection:
    """Decode only the latest bounded controller action bundle."""

    missing = object()
    thread = _read(snapshot, "thread", missing)
    if thread is missing:
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.MALFORMED,
            detail="thread snapshot is missing its thread envelope",
        )
    snapshot_thread_id = _read(thread, "id", missing)
    if snapshot_thread_id != thread_id:
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.MALFORMED,
            detail="thread identity is conflicting",
        )
    turns = _read(thread, "turns", None)
    if not isinstance(turns, list) or not turns or len(turns) > 64:
        return ControllerThreadInspection(
            thread_id, int(generation), ControllerThreadInspectionKind.MALFORMED, detail="turn snapshot is malformed"
        )
    latest = turns[-1]
    turn_id = _read(latest, "id", None)
    status = _enum_value(_read(latest, "status", None))
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or len(turn_id.encode("utf-8")) > 512
        or any(character.isspace() or ord(character) < 0x20 for character in turn_id)
        or not isinstance(status, str)
    ):
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.MALFORMED,
            detail="latest turn identity/status is malformed",
        )
    if status in {"inProgress", "in_progress", "active", "running"}:
        # A snapshot's active status is not ownership proof.  Only the exact
        # structured InvalidRequestError returned by the SDK read boundary can
        # establish that another writer currently owns the thread; otherwise
        # recovery must fail closed as ambiguous.
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.AMBIGUOUS,
            turn_id=turn_id,
            detail="latest turn is active without writer proof",
        )
    items = _read(latest, "items", None)
    # A completed controller turn has one and only one result item.  Failed or
    # interrupted turns may legitimately have no result items, but any item
    # they do expose still crosses the same closed-envelope checks below.
    if not isinstance(items, list) or len(items) > 4096 or (status == "completed" and len(items) != 1):
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.MALFORMED,
            turn_id=turn_id,
            detail="latest controller turn must contain exactly one item",
        )
    bundles: list[ModelFacingControllerActionBundle] = []
    malformed_agent_message = False
    for item in items:
        if not _closed_envelope_keys(item, frozenset({"root"})):
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.MALFORMED,
                turn_id=turn_id,
                detail="controller turn item envelope is not closed",
            )
        root = _read(item, "root", missing)
        if root is missing:
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.MALFORMED,
                turn_id=turn_id,
                detail="controller turn item is missing its root envelope",
            )
        if not _closed_envelope_keys(root, frozenset({"type", "text"})):
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.MALFORMED,
                turn_id=turn_id,
                detail="controller agentMessage root envelope is not closed",
            )
        item_type = _read(root, "type", None)
        if item_type != "agentMessage":
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.MALFORMED,
                turn_id=turn_id,
                detail="completed controller turn contains an unknown or missing item type",
            )
        text = _read(root, "text", None)
        if not isinstance(text, str) or len(text.encode("utf-8")) > 32 * 1024:
            malformed_agent_message = True
            continue
        try:
            candidate = ModelFacingControllerActionBundle.from_json_bytes(text)
        except (TypeError, ValueError):
            malformed_agent_message = True
            continue
        bundles.append(candidate)
    if status == "completed":
        matching = [
            candidate
            for candidate in bundles
            if candidate.decision_id == decision_id
            and candidate.generation == generation
            and (expected_revision is None or candidate.expected_revision == expected_revision)
        ]
        if malformed_agent_message:
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.MALFORMED,
                turn_id=turn_id,
                detail="completed turn contains malformed controller output",
            )
        if len(bundles) == 1 and len(matching) == 1:
            return ControllerThreadInspection(
                thread_id,
                int(generation),
                ControllerThreadInspectionKind.COMPLETED,
                turn_id=turn_id,
                bundle=matching[0],
            )
        return ControllerThreadInspection(
            thread_id,
            int(generation),
            ControllerThreadInspectionKind.MALFORMED if not bundles else ControllerThreadInspectionKind.AMBIGUOUS,
            turn_id=turn_id,
            detail="completed turn lacks one exact action bundle",
        )
    if status == "failed":
        return ControllerThreadInspection(
            thread_id, int(generation), ControllerThreadInspectionKind.FAILED, turn_id=turn_id
        )
    if status == "interrupted":
        return ControllerThreadInspection(
            thread_id, int(generation), ControllerThreadInspectionKind.INTERRUPTED, turn_id=turn_id
        )
    return ControllerThreadInspection(
        thread_id,
        int(generation),
        ControllerThreadInspectionKind.AMBIGUOUS,
        turn_id=turn_id,
        detail="unknown turn status",
    )


def _closed_envelope_keys(value: object, expected: frozenset[str]) -> bool:
    """Return whether an SDK envelope exposes exactly the expected fields."""

    if isinstance(value, Mapping):
        keys = tuple(value.keys())
    else:
        try:
            keys = tuple(vars(value).keys())
        except TypeError:
            return False
    return all(isinstance(key, str) for key in keys) and frozenset(keys) == expected and len(keys) == len(expected)


class _SdkSurface(Protocol):
    Codex: Callable[..., _SdkClient]
    CodexConfig: Callable[..., object]
    Sandbox: Any
    ApprovalMode: Any
    SkillInput: Callable[..., object] | None
    TextInput: Callable[..., object] | None
    LocalImageInput: Callable[..., object] | None
    ReasoningEffort: Any
    version: str


class WakeDeliveryUnavailable(RuntimeError):
    """A source wake could not reach a source turn identity."""


class WakeDeliveryAmbiguous(RuntimeError):
    """The source SDK call crossed its turn boundary without a durable id."""


class ResponseChainInvalid(TerminalFailureAfterIdentity):
    """The provider rejected the exact SDK continuation chain."""


class SchemaOutputInvalid(TerminalFailureAfterIdentity):
    """A completed SDK turn returned an output that violates its bound schema."""


class StaleTurnIdentity(TerminalFailureAfterIdentity):
    """A live control operation named a turn that is no longer retained."""


def _sdk_transient_error(value: BaseException) -> bool:
    """Recognize only the pinned SDK's typed transient failures.

    The helper is imported from the installed SDK rather than approximated by
    exception names or message text.  If the SDK does not expose the typed
    predicate, no automatic retry is authorized.
    """

    try:
        from openai_codex.errors import TransportClosedError, is_retryable_error
    except (ImportError, AttributeError):
        return False
    return isinstance(value, TransportClosedError) or bool(is_retryable_error(value))


def _classify_sdk_failure(value: BaseException, *, after_identity: bool) -> CodexFlowError:
    """Map SDK failures into the closed domain classes used by recovery."""

    if isinstance(value, UnsupportedCapability):
        return value
    if _sdk_transient_error(value):
        if after_identity:
            return TransientFailureAfterIdentity("SDK transport closed after thread identity")
        return TransportFailureBeforeIdentity("SDK transport closed before thread identity")
    # The pinned SDK currently exposes RPC errors rather than dedicated auth
    # and permission classes.  Classify those explicit classes by type only;
    # arbitrary text is never a retry selector.
    class_name = type(value).__name__
    if class_name in {"AuthenticationError", "UnauthorizedError", "AuthError"}:
        from ..domain import AuthenticationFailure

        return AuthenticationFailure("SDK authentication failed")
    if class_name in {"PermissionError", "ForbiddenError", "PermissionDeniedError"}:
        from ..domain import PermissionFailure

        return PermissionFailure("SDK permission was denied")
    if class_name in {"ProfileError", "ConfigurationError"}:
        from ..domain import ProfileFailure

        return ProfileFailure("SDK profile is invalid")
    if class_name in {"IntegrityError", "WorkspaceIntegrityError"}:
        from ..domain import IntegrityFailure

        return IntegrityFailure("SDK workspace or identity integrity failed")
    if after_identity:
        return UnknownSdkFailureAfterIdentity("SDK operation failed after thread identity")
    return UnknownSdkFailureBeforeIdentity("SDK operation failed before thread identity")


def _is_invalid_previous_response_error(value: object) -> bool:
    """Recognize only the provider's structured invalid-chain error."""

    candidates = tuple(value.args) if isinstance(value, BaseException) else (value,)
    for candidate in candidates:
        decoded: object = candidate
        if isinstance(candidate, str):
            try:
                decoded = strict_json_loads(candidate, max_bytes=16_384)
            except ValueError:
                continue
        if not isinstance(decoded, Mapping):
            continue
        error = decoded.get("error")
        if (
            decoded.get("type") == "error"
            and decoded.get("status") == 400
            and isinstance(error, Mapping)
            and error.get("type") == "invalid_request_error"
            and error.get("message") == "Invalid `previous_response_id`."
        ):
            return True
    return False


_RETRYABLE_PROVIDER_STATUSES = frozenset({429, 500, 502, 503, 504})
_CANONICAL_PROVIDER_STATUS_ERROR = re.compile(
    r"\Aunexpected status (?P<status>[1-5][0-9]{2}) [^:\r\n]{1,64}: [^\r\n]{1,512}\Z"
)
_CANONICAL_USAGE_LIMIT_RESET = re.compile(
    r"try again at (?P<hour>1[0-2]|[1-9]):(?P<minute>[0-5][0-9]) (?P<period>AM|PM)\.\Z"
)


def _temporary_usage_limit_retry_at(error: object, *, now: datetime | None = None) -> str | None:
    """Decode only the pinned typed usage-limit variant plus its reset suffix.

    The app-server does not expose ``Retry-After`` in its generated TurnError
    model.  The typed ``usageLimitExceeded`` discriminator owns
    classification; the closed message suffix is used only to recover the
    explicit local reset clock.  A usage/quota error without that suffix is
    never selected for automatic retry.
    """

    info = _read(error, "codex_error_info") or _read(error, "codexErrorInfo")
    root = _read(info, "root", info)
    if _enum_value(root) != "usageLimitExceeded":
        return None
    message = _read(error, "message")
    if not isinstance(message, str) or len(message.encode("utf-8")) > 1024:
        return None
    match = _CANONICAL_USAGE_LIMIT_RESET.search(message)
    if match is None:
        return None
    local_now = (now or datetime.now().astimezone()).astimezone()
    hour = int(match.group("hour")) % 12
    if match.group("period") == "PM":
        hour += 12
    retry_at = local_now.replace(hour=hour, minute=int(match.group("minute")), second=0, microsecond=0)
    if retry_at <= local_now:
        retry_at += timedelta(days=1)
    # Cross the displayed minute boundary before retrying.  The five-second
    # cushion is deterministic and keeps all codex-lb clients from hitting the
    # exact reset instant while remaining bounded to one day.
    retry_at += timedelta(seconds=5)
    if retry_at - local_now > timedelta(days=1, seconds=5):
        return None
    return retry_at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _turn_error_is_transient(error: object) -> bool:
    """Recognize only typed or canonical app-server transient failures."""

    info = _read(error, "codex_error_info") or _read(error, "codexErrorInfo")
    root = _read(info, "root", info)
    info_value = _enum_value(root)
    if info_value in {"serverOverloaded", "internalServerError"}:
        return True
    for snake, camel in (
        ("http_connection_failed", "httpConnectionFailed"),
        ("response_stream_connection_failed", "responseStreamConnectionFailed"),
        ("response_stream_disconnected", "responseStreamDisconnected"),
        ("response_too_many_failed_attempts", "responseTooManyFailedAttempts"),
    ):
        detail = _read(root, snake) or _read(root, camel)
        status = _read(detail, "http_status_code") or _read(detail, "httpStatusCode")
        if isinstance(status, int) and not isinstance(status, bool) and status in _RETRYABLE_PROVIDER_STATUSES:
            return True
    # Codex app-server 0.147.0 currently emits some upstream 5xx failures as
    # the typed ``other`` variant and retains the status only in this closed,
    # canonical message form. Do not treat arbitrary prose or other codes as
    # retry authority.
    if info_value != "other":
        return False
    message = _read(error, "message")
    if not isinstance(message, str):
        return False
    match = _CANONICAL_PROVIDER_STATUS_ERROR.fullmatch(message)
    return match is not None and int(match.group("status")) in _RETRYABLE_PROVIDER_STATUSES


class LiveTurnHandle:
    """Typed capability handle for one exact in-flight SDK turn.

    The raw SDK object never crosses the adapter boundary.  ``steer`` and
    ``interrupt`` are deliberately synchronous: the SDK acknowledgement only
    proves that the command reached this handle; terminal evidence still comes
    from the normal stream.
    """

    def __init__(self, adapter: CodexSdkAdapter, thread: ThreadIdentity, turn_id: str, raw_turn: _SdkTurn) -> None:
        self._adapter = adapter
        self.thread_id = thread
        self.turn_id = turn_id
        self._raw_turn = raw_turn

    def steer(self, input: str | SkillInput) -> None:
        if isinstance(input, str) and (
            not input.strip() or len(input.encode("utf-8")) > STEER_TEXT_MAX_BYTES or "\x00" in input
        ):
            raise ValueError("steer text exceeds its byte limit")
        try:
            method = self._raw_turn.steer
        except AttributeError as exc:
            raise UnsupportedCapability("installed SDK does not expose TurnHandle.steer") from exc
        try:
            method(_wire_input(self._adapter._sdk, input))
        except UnsupportedCapability:
            raise
        except Exception as exc:
            raise _classify_sdk_failure(exc, after_identity=True) from exc

    def interrupt(self) -> None:
        try:
            method = self._raw_turn.interrupt
        except AttributeError as exc:
            raise UnsupportedCapability("installed SDK does not expose TurnHandle.interrupt") from exc
        try:
            method()
        except UnsupportedCapability:
            raise
        except Exception as exc:
            raise _classify_sdk_failure(exc, after_identity=True) from exc

    def stream(self) -> Iterable[Any]:
        """Expose the bounded raw event iterator through the typed handle."""

        return self._raw_turn.stream()


@dataclass(frozen=True, slots=True)
class CodexSdkConfig:
    """Explicit configuration for one SDK connection and thread."""

    model: str
    reasoning_effort: ReasoningEffort
    sandbox: Sandbox | None = Sandbox.READ_ONLY
    cwd: Path | None = None
    ephemeral: bool = False
    native_runtime: NativeRuntimeConfig | None = None
    permission_mode: NativePermissionMode | None = None
    effective_permission: NativePermissionAuthority | None = None
    # Detached H6-E workers are leaf workers.  The stable SDK has no generic
    # lifecycle-tool allowlist, so the harness binds the two documented
    # configuration switches that remove collaboration and agent tools before
    # the runtime or thread is created.  Ordinary H1-H5 callers retain the
    # historical default (False).
    leaf_worker: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must be explicit and non-empty")
        if self.sandbox not in {None, Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE, Sandbox.FULL_ACCESS}:
            raise ValueError("controller SDK execution requires an explicit supported sandbox")
        if self.cwd is not None and not self.cwd.is_dir():
            raise ValueError(f"SDK working directory does not exist: {self.cwd}")
        if self.permission_mode is not None and self.native_runtime is None:
            raise ValueError("native permission inheritance requires a validated native runtime profile")
        if self.permission_mode is not None and self.effective_permission is None:
            raise ValueError("native permission inheritance requires durable effective authority")
        if self.permission_mode is not None and self.sandbox is not None:
            raise ValueError("native permission mode cannot carry a separate sandbox override")
        if self.effective_permission is not None and self.permission_mode is None:
            raise ValueError("effective native authority requires a native permission mode")
        if self.native_runtime is not None and self.cwd is None:
            raise ValueError("native runtime execution requires an explicit SDK cwd")
        if not isinstance(self.leaf_worker, bool):
            raise ValueError("leaf_worker must be boolean")


# These are the official Codex configuration keys for removing the two
# nested-lifecycle surfaces.  Keep them immutable and explicit: prompt prose
# is not an enforcement boundary.
LEAF_WORKER_CONFIG_OVERRIDES: tuple[str, ...] = (
    "agents.enabled=false",
    "features.multi_agent=false",
)
LEAF_WORKER_THREAD_CONFIG: JsonObject = {
    "agents": {"enabled": False},
    "features": {"multi_agent": False},
}
LEAF_WORKER_ALLOWED_OPERATIONS: frozenset[str] = frozenset({"workspace", "validation", "submit_result"})


def _leaf_runtime_supported(sdk: _SdkSurface) -> bool:
    """Prove the installed SDK supports the shared leaf runtime overlay.

    ``config_overrides`` is the stable SDK's pre-start runtime config seam.
    Authentication and session state intentionally come from the inherited
    standard Codex process environment; an ``env`` override is neither needed
    nor accepted as proof of the shared-session route.
    """

    try:
        config_parameters = inspect.signature(sdk.CodexConfig).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    if "config_overrides" not in config_parameters:
        return False
    return True


@dataclass(frozen=True, slots=True)
class NativeRuntimeConfig:
    """Native agent semantics for either a legacy projection or shared session."""

    runtime_home: Path | None
    native_profile: NativeProfileProjection
    shared_session: bool = False

    def __post_init__(self) -> None:
        if self.runtime_home is not None and not self.runtime_home.is_absolute():
            raise ValueError("private native runtime home must be absolute")

    def prepare(self) -> None:
        if self.shared_session:
            return
        if self.runtime_home is None:  # pragma: no cover - constructor invariant
            raise ValueError("legacy native runtime requires a private runtime home")
        self.native_profile.prepare_runtime_home(self.runtime_home)

    @property
    def environment(self) -> dict[str, str]:
        if self.shared_session:
            return {}
        if self.runtime_home is None:  # pragma: no cover - constructor invariant
            raise ValueError("legacy native runtime requires a private runtime home")
        environment = dict(self.native_profile.ephemeral_environment)
        # CODEX_HOME is reserved by the controller's SDK process.  A literal
        # native MCP value with that name is delivered through the projection's
        # deterministic collision alias and remapped only in the MCP child.
        environment["CODEX_HOME"] = os.fspath(self.runtime_home)
        return environment

    @classmethod
    def shared(cls, native_profile: NativeProfileProjection) -> NativeRuntimeConfig:
        """Bind the inherited standard Codex session without copying a home."""

        return cls(None, native_profile, shared_session=True)


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
            CodexConfig=package.CodexConfig,
            Sandbox=package.Sandbox,
            ApprovalMode=package.ApprovalMode,
            SkillInput=package.SkillInput,
            TextInput=package.__dict__.get("TextInput") or types.__dict__.get("TextInput"),
            LocalImageInput=package.__dict__.get("LocalImageInput") or types.__dict__.get("LocalImageInput"),
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
    value = raw_turn.id
    if not isinstance(value, str) or not value or "\x00" in value or any(character.isspace() for character in value):
        raise ValueError("SDK turn handle did not contain a valid bounded id")
    if len(value.encode("utf-8")) > 512:
        raise ValueError("SDK turn handle id exceeds its byte limit")
    return value


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


def _wire_turn_input(sdk: _SdkSurface, value: str | SkillInput, local_image_inputs: tuple[LocalImageInput, ...]) -> Any:
    """Project one prompt plus local paths into the SDK's official input list."""

    if not local_image_inputs:
        return _wire_input(sdk, value)
    try:
        image_constructor = sdk.LocalImageInput
    except AttributeError as exc:
        raise UnsupportedCapability("installed SDK does not expose LocalImageInput") from exc
    if not callable(image_constructor):
        raise UnsupportedCapability("installed SDK does not expose LocalImageInput")
    if isinstance(value, str):
        try:
            text_constructor = sdk.TextInput
        except AttributeError as exc:
            raise UnsupportedCapability("installed SDK does not expose TextInput") from exc
        if not callable(text_constructor):
            raise UnsupportedCapability("installed SDK does not expose TextInput")
        first = text_constructor(text=value)
    else:
        first = _wire_input(sdk, value)
    return [first, *(image_constructor(path=item.path) for item in local_image_inputs)]


def _event_from_notification(event: _SdkNotification, sequence: int) -> LifecycleEvent:
    if not event.method:
        raise TerminalFailureAfterIdentity("SDK emitted a lifecycle event without a method")
    payload = event.payload
    turn_id = _read(payload, "turn_id") or _read(payload, "turnId")
    if turn_id is None:
        turn = _read(payload, "turn")
        turn_id = _read(turn, "id")
    tool_summary = _tool_call_summary(event)
    raw_text = tool_summary if tool_summary is not None else _response_from_notification(event)
    text = redact_diagnostic_text(raw_text) if raw_text is not None else None
    return LifecycleEvent(sequence=sequence, method=event.method, turn_id=turn_id, text=text)


def _status_from_notification(event: Any) -> tuple[str | None, str | None, bool, str | None]:
    payload = cast(_SdkNotification, event).payload
    turn = _read(payload, "turn")
    status = _enum_value(_read(turn, "status"))
    error = _read(turn, "error")
    message = _read(error, "message")
    return (
        status if isinstance(status, str) else None,
        message if isinstance(message, str) else None,
        _turn_error_is_transient(error),
        _temporary_usage_limit_retry_at(error),
    )


def _response_from_notification(event: Any) -> str | None:
    payload = cast(_SdkNotification, event).payload
    item = _read(payload, "item")
    root = _read(item, "root", item)
    item_type = _read(root, "type")
    if item_type is not None and item_type != "agentMessage":
        return None
    text = _read(root, "text")
    return text if isinstance(text, str) else None


def _tool_call_summary(event: _SdkNotification) -> str | None:
    """Return a useful, body-free summary for a tool item notification."""

    if event.method not in {"item/started", "item/completed"}:
        return None
    item = _read(event.payload, "item")
    root = _read(item, "root", item)
    item_type = _read(root, "type")
    if not isinstance(item_type, str):
        return None
    labels = {
        "commandExecution": "command execution",
        "mcpToolCall": "MCP tool",
        "webSearchCall": "web search",
        "fileSearchCall": "file search",
    }
    label = labels.get(item_type)
    if label is None:
        return None
    status = _summary_atom(_enum_value(_read(root, "status")))
    if status is None:
        status = "started" if event.method == "item/started" else "completed"
    if item_type == "mcpToolCall":
        server = _summary_atom(_read(root, "server"))
        tool = _summary_atom(_read(root, "tool"))
        if server is not None and tool is not None:
            identity = f" {server}/{tool}"
            return f"{label}{identity} {status}"
    return f"{label} {status}"


def _summary_atom(value: object) -> str | None:
    """Allow only identifier-like tool metadata into diagnostic text."""

    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) is None:
        return None
    return value


def _provider_output_schema(schema: Schema | None) -> JsonObject | None:
    """Project the canonical schema onto the provider's supported subset.

    This is a positive structural projection of the documented Structured
    Outputs subset, not a list of errors observed from individual schemas.  The
    canonical schema remains the sole acceptance authority:
    ``_decode_schema_output`` validates the returned value against the complete
    local Draft 2020-12 contract.
    """

    if schema is None:
        return None
    validate_output_schema(schema)

    common_keys = frozenset({"type", "description", "enum"})
    object_keys = frozenset({"properties", "required", "additionalProperties"})
    array_keys = frozenset({"items", "minItems", "maxItems"})
    number_keys = frozenset({"multipleOf", "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum"})

    def provider_pattern(pattern: object) -> str | None:
        if not isinstance(pattern, str):
            return None
        # Structured Outputs accepts ordinary regular expressions, while the
        # provider grammar excludes extension groups (including lookarounds)
        # and backreferences.  Keep a deliberately small structural subset.
        escaped = False
        for index, character in enumerate(pattern):
            if escaped:
                if character.isdigit() or character in {"g", "k"}:
                    return None
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == "(" and index + 1 < len(pattern) and pattern[index + 1] == "?":
                return None
        return None if escaped else pattern

    def project(node: object, *, root: bool = False) -> object:
        if not isinstance(node, Mapping):
            return thaw_json(node)

        projected: dict[str, object] = {key: thaw_json(value) for key, value in node.items() if key in common_keys}
        if "const" in node:
            constant = thaw_json(node["const"])
            projected["enum"] = [constant]
            if "type" not in projected:
                inferred_type = (
                    "null"
                    if constant is None
                    else "boolean"
                    if isinstance(constant, bool)
                    else "integer"
                    if isinstance(constant, int)
                    else "number"
                    if isinstance(constant, float)
                    else "string"
                    if isinstance(constant, str)
                    else None
                )
                if inferred_type is None:
                    raise TerminalFailureAfterIdentity("provider output schema cannot project a container const")
                projected["type"] = inferred_type
        branches = node.get("oneOf")
        if isinstance(branches, list | tuple):
            if root:
                raise TerminalFailureAfterIdentity("provider output schema cannot project a root union")
            projected["anyOf"] = [project(branch) for branch in branches]
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            projected["properties"] = {str(name): project(child) for name, child in properties.items()}
            for key in object_keys - {"properties"}:
                if key in node:
                    projected[key] = thaw_json(node[key])
        elif node.get("type") == "object":
            raise TerminalFailureAfterIdentity("provider output schema cannot project dynamic object keys")
        items = node.get("items")
        if isinstance(items, Mapping):
            projected["items"] = project(items)
            for key in array_keys - {"items"}:
                if key in node:
                    projected[key] = thaw_json(node[key])
        for key in number_keys:
            if key in node:
                projected[key] = thaw_json(node[key])
        pattern = provider_pattern(node.get("pattern"))
        if pattern is not None:
            projected["pattern"] = pattern
        if "format" in node:
            projected["format"] = thaw_json(node["format"])
        return projected

    return cast(JsonObject, project(schema, root=True))


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
        response_bytes = response.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TerminalFailureAfterIdentity("schema-bounded turn returned invalid JSON") from exc
    if len(response_bytes) > MAX_STRUCTURED_OUTPUT_BYTES:
        raise TerminalFailureAfterIdentity("schema-bounded turn output exceeds the byte limit")
    try:
        decoded = strict_json_loads(response)
    except ValueError as exc:
        raise TerminalFailureAfterIdentity("schema-bounded turn returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise TerminalFailureAfterIdentity("schema-bounded turn returned a non-object JSON value")
    try:
        validate_structured_output(decoded, schema)
    except ValueError as exc:
        detail = redact_diagnostic_text(str(exc), limit=256)
        raise TerminalFailureAfterIdentity(f"schema-bounded turn output does not match its schema: {detail}") from exc
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
        self._client_factory = client_factory or self._production_client
        self._client: _SdkClient | None = None
        self._threads: dict[str, _SdkThread] = {}
        self._turns: dict[tuple[str, str], _SdkTurn] = {}

    def _production_client(self) -> _SdkClient:
        runtime = self.config.native_runtime
        if self.config.leaf_worker and not _leaf_runtime_supported(self._sdk):
            raise UnsupportedCapability(
                "installed SDK cannot prove a leaf-worker runtime override; "
                "agents.enabled=false and features.multi_agent=false are required"
            )
        config_overrides = LEAF_WORKER_CONFIG_OVERRIDES if self.config.leaf_worker else ()
        if runtime is None:
            if self.config.leaf_worker:
                try:
                    sdk_config = self._sdk.CodexConfig(
                        cwd=os.fspath(self.config.cwd) if self.config.cwd is not None else None,
                        config_overrides=config_overrides,
                    )
                    return self._sdk.Codex(config=sdk_config)
                except (AttributeError, ImportError, TypeError, ValueError) as exc:
                    raise UnsupportedCapability(
                        "installed SDK cannot establish the shared leaf-worker runtime"
                    ) from exc
            return self._sdk.Codex()
        try:
            if runtime.shared_session:
                sdk_config = self._sdk.CodexConfig(
                    cwd=os.fspath(self.config.cwd),
                    config_overrides=config_overrides,
                )
                return self._sdk.Codex(config=sdk_config)
            runtime.prepare()
            sdk_config = self._sdk.CodexConfig(
                cwd=os.fspath(self.config.cwd),
                env=runtime.environment,
                config_overrides=config_overrides,
            )
            return self._sdk.Codex(config=sdk_config)
        except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
            raise UnsupportedCapability("installed SDK cannot establish the native runtime profile") from exc

    def _permission_kwargs(self) -> dict[str, Any]:
        mode = self.config.permission_mode
        if mode is None:
            if self.config.sandbox is None:  # pragma: no cover - constructor contract for native mode
                raise UnsupportedCapability("explicit SDK sandbox is missing")
            return {
                "approval_mode": _wire_approval_mode(self._sdk),
                "sandbox": _wire_sandbox(self._sdk, self.config.sandbox),
            }
        runtime = self.config.native_runtime
        if runtime is None:  # pragma: no cover - constructor invariant
            raise UnsupportedCapability("native permission inheritance has no validated profile")
        native = runtime.native_profile.effective_authority(NativePermissionMode.INHERIT_NATIVE)
        effective = self.config.effective_permission
        if effective is None:  # pragma: no cover - constructor invariant
            raise UnsupportedCapability("durable effective native authority is missing")
        if effective.meet(native) != effective:
            raise UnsupportedCapability("durable effective authority would broaden the current native profile")
        kwargs: dict[str, Any] = {}
        if effective.sandbox_mode != native.sandbox_mode:
            sandbox = {
                "read-only": Sandbox.READ_ONLY,
                "workspace-write": Sandbox.WORKSPACE_WRITE,
                "danger-full-access": Sandbox.FULL_ACCESS,
            }[effective.sandbox_mode]
            kwargs["sandbox"] = _wire_sandbox(self._sdk, sandbox)
        if effective.approval_policy != native.approval_policy:
            if effective.approval_policy != "never":
                raise UnsupportedCapability("installed SDK cannot retain the prior native approval restriction")
            kwargs["approval_mode"] = _wire_approval_mode(self._sdk)
        return kwargs

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
        self._turns.clear()
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
        if self.config.leaf_worker and not _leaf_runtime_supported(self._sdk):
            raise UnsupportedCapability(
                "installed SDK cannot prove a leaf-worker runtime override; "
                "agents.enabled=false and features.multi_agent=false are required"
            )
        try:
            client = self._client_factory()
            self._client = client
            if self.config.leaf_worker:
                self._assert_leaf_thread_surface(client)
            raw_thread = client.thread_start(
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                model=self.config.model,
                ephemeral=self.config.ephemeral,
                config=(dict(LEAF_WORKER_THREAD_CONFIG) if self.config.leaf_worker else None),
                **self._permission_kwargs(),
            )
            identity = _identity(raw_thread)
        except UnsupportedCapability:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise _classify_sdk_failure(exc, after_identity=False) from exc
        self._threads[identity.id] = raw_thread
        return identity

    def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity:
        """Resume exactly the addressable thread and reject identity changes."""

        if self.config.leaf_worker and not _leaf_runtime_supported(self._sdk):
            raise UnsupportedCapability("installed SDK cannot prove a leaf-worker runtime override on resume")
        created_client = self._client is None
        try:
            if self._client is None:
                self._client = self._client_factory()
            client = self._require_client()
            raw_thread = client.thread_resume(
                thread.id,
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                model=self.config.model,
                config=(dict(LEAF_WORKER_THREAD_CONFIG) if self.config.leaf_worker else None),
                **self._permission_kwargs(),
            )
            resumed = _identity(raw_thread)
        except UnsupportedCapability:
            if created_client:
                self.close()
            raise
        except Exception as exc:
            if created_client:
                self.close()
            if _is_invalid_previous_response_error(exc):
                raise ResponseChainInvalid(
                    f"SDK response chain is invalid after thread identity {thread.id!r}"
                ) from exc
            raise _classify_sdk_failure(exc, after_identity=True) from exc
        if resumed.id != thread.id:
            if created_client:
                self.close()
            raise TerminalFailureAfterIdentity(
                f"SDK resume changed thread identity from {thread.id!r} to {resumed.id!r}"
            )
        self._threads[thread.id] = raw_thread
        return resumed

    @staticmethod
    def _assert_leaf_thread_surface(client: _SdkClient) -> None:
        """Fail closed unless the SDK exposes the per-thread config seam."""

        try:
            parameters = inspect.signature(client.thread_start).parameters
        except (AttributeError, TypeError, ValueError) as exc:
            raise UnsupportedCapability("installed SDK cannot prove per-thread leaf-worker configuration") from exc
        if "config" not in parameters and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            raise UnsupportedCapability("installed SDK thread_start has no per-thread config seam")
        try:
            resume_parameters = inspect.signature(client.thread_resume).parameters
        except (AttributeError, TypeError, ValueError) as exc:
            raise UnsupportedCapability("installed SDK cannot prove leaf-worker resume configuration") from exc
        if "config" not in resume_parameters and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in resume_parameters.values()
        ):
            raise UnsupportedCapability("installed SDK thread_resume has no per-thread config seam")

    def archive_thread(self, thread: ThreadIdentity) -> None:
        """Archive a thread created by a compatibility sentinel after proof."""

        client = self._require_client()
        try:
            client.thread_archive(thread.id)
        except Exception as exc:
            raise TerminalFailureAfterIdentity(f"SDK archive failed after thread identity {thread.id!r}") from exc

    def archive_persisted_thread(self, thread: ThreadIdentity) -> None:
        """Archive one already-created thread without opening a new writer."""

        created_client = self._client is None
        if created_client:
            try:
                self._client = self._client_factory()
            except UnsupportedCapability:
                raise
            except Exception as exc:
                raise TerminalFailureAfterIdentity("SDK archive could not connect to the shared session") from exc
        try:
            self.archive_thread(thread)
        finally:
            if created_client:
                self.close()

    def inspect_persisted_thread(self, thread: ThreadIdentity) -> ThreadInspection:
        """Read one persisted thread snapshot without creating a writer."""

        if not thread.id or any(character.isspace() for character in thread.id):
            raise ValueError("persisted thread identity is invalid")
        created_client = self._client is None
        if created_client:
            try:
                self._client = self._client_factory()
            except Exception as exc:
                raise TransportFailureBeforeIdentity("SDK persisted-thread inspection could not connect") from exc
        client = self._require_client()
        try:
            try:
                snapshot = _sdk_thread_read(client, thread.id)
            except UnsupportedCapability:
                raise
            except Exception as exc:
                if _active_writer_error(exc):
                    return ThreadInspection(thread.id, ThreadInspectionKind.ACTIVE_WRITER)
                # Do not classify arbitrary text or unrelated RPC codes as a
                # writer; recovery must close as ambiguous instead.
                return ThreadInspection(thread.id, ThreadInspectionKind.AMBIGUOUS, detail="thread/read failed")
            return _terminal_from_thread_snapshot(snapshot, thread.id)
        finally:
            if created_client:
                self.close()

    def read_thread(self, thread: ThreadIdentity) -> ThreadInspection:
        """Read one persisted SDK thread through the typed adapter boundary."""

        return self.inspect_persisted_thread(thread)

    def read_conversation_history(self, request: ConversationHistoryRequest) -> ConversationHistoryPage:
        """Read one bounded page from the official persisted thread snapshot."""

        if not isinstance(request, ConversationHistoryRequest):
            raise TypeError("conversation history request must be typed")
        created_client = self._client is None
        if created_client:
            try:
                self._client = self._client_factory()
            except Exception:
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.UNAVAILABLE,
                    None,
                    reason="shared SDK session unavailable",
                )
        try:
            try:
                snapshot = _sdk_thread_read(self._require_client(), request.thread_id.id)
                return _history_page(request, snapshot)
            except IncompleteConversationHistory as exc:
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.SOURCE_INCOMPLETE,
                    None,
                    reason=str(exc),
                )
            except OverflowError as exc:
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.SOURCE_TOO_LARGE,
                    None,
                    reason=str(exc),
                )
            except (ValueError, TypeError, KeyError) as exc:
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.MALFORMED,
                    None,
                    reason=str(exc),
                )
            except Exception as exc:
                class_name = type(exc).__name__
                status = (
                    ConversationHistoryStatus.DELETED
                    if class_name in {"NotFoundError", "ThreadNotFoundError", "ResourceNotFoundError"}
                    else ConversationHistoryStatus.PERMISSION_DENIED
                    if class_name in {"AuthenticationError", "PermissionError", "UnauthorizedError", "ForbiddenError"}
                    else ConversationHistoryStatus.UNAVAILABLE
                )
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    status,
                    None,
                    reason=(
                        "history deleted" if status is ConversationHistoryStatus.DELETED else "history read unavailable"
                    ),
                )
        finally:
            if created_client:
                self.close()

    def inspect_controller_thread(
        self,
        thread: ThreadIdentity,
        *,
        decision_id: ControllerDecisionId | str,
        generation: Generation | int,
        expected_revision: int | None = None,
    ) -> ControllerThreadInspection:
        """Perform one authoritative, read-only controller-thread inspection."""

        decision = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        created_client = self._client is None
        if created_client:
            try:
                self._client = self._client_factory()
            except Exception:
                return ControllerThreadInspection(
                    thread.id,
                    int(generation_value),
                    ControllerThreadInspectionKind.UNAVAILABLE,
                    detail="shared SDK session unavailable",
                )
        try:
            try:
                snapshot = _sdk_thread_read(self._require_client(), thread.id)
            except UnsupportedCapability:
                raise
            except Exception as exc:
                class_name = type(exc).__name__
                if class_name in {"NotFoundError", "ThreadNotFoundError", "ResourceNotFoundError"}:
                    return ControllerThreadInspection(
                        thread.id,
                        int(generation_value),
                        ControllerThreadInspectionKind.UNAVAILABLE,
                        detail="persisted controller thread is unavailable",
                    )
                if _active_writer_error(exc):
                    return ControllerThreadInspection(
                        thread.id,
                        int(generation_value),
                        ControllerThreadInspectionKind.ACTIVE,
                        detail="active writer is proven",
                    )
                return ControllerThreadInspection(
                    thread.id,
                    int(generation_value),
                    ControllerThreadInspectionKind.AMBIGUOUS,
                    detail="thread read is ambiguous",
                )
            return _controller_from_thread_snapshot(
                snapshot,
                thread.id,
                decision,
                generation_value,
                expected_revision=expected_revision,
            )
        finally:
            if created_client:
                self.close()

    def run_turn(
        self,
        thread: ThreadIdentity,
        input: str | SkillInput,
        *,
        local_image_inputs: tuple[LocalImageInput, ...] = (),
        output_schema: Schema | None = None,
        event_callback: Callable[[LifecycleEvent], None] | None = None,
        turn_callback: Callable[[str], None] | None = None,
    ) -> TurnObservation:
        """Run one logical turn; recovery owns any later provider continuation."""

        return self._run_turn_once(
            thread,
            input,
            local_image_inputs=local_image_inputs,
            output_schema=output_schema,
            event_callback=event_callback,
            turn_callback=turn_callback,
        )

    def _run_turn_once(
        self,
        thread: ThreadIdentity,
        input: str | SkillInput,
        *,
        local_image_inputs: tuple[LocalImageInput, ...] = (),
        output_schema: Schema | None = None,
        event_callback: Callable[[LifecycleEvent], None] | None = None,
        turn_callback: Callable[[str], None] | None = None,
    ) -> TurnObservation:
        """Run one physical SDK turn and normalize its terminal observation."""

        handle = self.start_turn(
            thread,
            input,
            local_image_inputs=local_image_inputs,
            output_schema=output_schema,
        )
        try:
            if turn_callback is not None:
                try:
                    turn_callback(handle.turn_id)
                except Exception as exc:
                    raise TerminalFailureAfterIdentity("SDK turn failed while binding callback") from exc
            raw_turn = handle._raw_turn
            turn_id = handle.turn_id
            events: list[LifecycleEvent] = []
            final_response: str | None = None
            status: str | None = None
            error: str | None = None
            transient_error = False
            rate_limit_retry_at: str | None = None
            for sequence, raw_event in enumerate(raw_turn.stream()):
                event = _event_from_notification(raw_event, sequence)
                events.append(event)
                if event_callback is not None:
                    try:
                        event_callback(event)
                    except Exception as exc:
                        raise TerminalFailureAfterIdentity("SDK lifecycle callback failed") from exc
                if event.method == "item/completed":
                    candidate = _response_from_notification(raw_event)
                    if candidate is not None:
                        final_response = candidate
                if event.method == "turn/completed":
                    status, error, transient_error, rate_limit_retry_at = _status_from_notification(raw_event)
            if status is None:
                raise TerminalFailureAfterIdentity(f"SDK turn {turn_id!r} ended without a turn/completed event")
            if status != "completed":
                if _is_invalid_previous_response_error(error):
                    raise ResponseChainInvalid(f"SDK response chain is invalid after thread identity {thread.id!r}")
                if rate_limit_retry_at is not None:
                    raise TemporaryRateLimitAfterIdentity(
                        f"SDK turn reached a temporary usage limit after thread identity {thread.id!r}",
                        retry_at=rate_limit_retry_at,
                    )
                if transient_error:
                    raise TransientFailureAfterIdentity(
                        f"SDK turn failed with a retryable provider status after thread identity {thread.id!r}"
                    )
                raise TerminalFailureAfterIdentity(error or f"SDK turn {turn_id!r} ended with status {status!r}")
            try:
                structured = _decode_schema_output(final_response, output_schema)
            except TerminalFailureAfterIdentity as exc:
                raise SchemaOutputInvalid(str(exc)) from exc
            return TurnObservation(
                thread_id=thread,
                turn_id=turn_id,
                status=status,
                final_response=final_response,
                structured_output=structured,
                events=tuple(events),
                error=error,
            )
        except (TerminalFailureAfterIdentity, TransientFailureAfterIdentity):
            raise
        except UnsupportedCapability:
            raise
        except Exception as exc:
            if _is_invalid_previous_response_error(exc):
                raise ResponseChainInvalid(
                    f"SDK response chain is invalid after thread identity {thread.id!r}"
                ) from exc
            raise _classify_sdk_failure(exc, after_identity=True) from exc
        finally:
            # A completed or failed stream is no longer a live control target;
            # retaining it would permit post-terminal steer/interrupt calls.
            self._turns.pop((thread.id, handle.turn_id), None)

    def start_turn(
        self,
        thread: ThreadIdentity,
        input: str | SkillInput,
        *,
        local_image_inputs: tuple[LocalImageInput, ...] = (),
        output_schema: Schema | None = None,
    ) -> LiveTurnHandle:
        """Start and retain one live SDK turn before consuming its stream."""

        raw_thread = self._threads.get(thread.id)
        if raw_thread is None:
            raise CodexFlowError(f"unknown SDK thread identity: {thread.id}")
        if any(thread_id == thread.id for thread_id, _turn_id in self._turns):
            raise TerminalFailureAfterIdentity(f"SDK thread {thread.id!r} already has a retained live turn")
        try:
            raw_turn = raw_thread.turn(
                _wire_turn_input(self._sdk, input, local_image_inputs),
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                effort=_wire_effort(self._sdk, self.config.reasoning_effort),
                model=self.config.model,
                output_schema=_provider_output_schema(output_schema),
                **self._permission_kwargs(),
            )
            turn_id = _turn_id(raw_turn)
        except TerminalFailureAfterIdentity:
            raise
        except UnsupportedCapability:
            raise
        except Exception as exc:
            if _is_invalid_previous_response_error(exc):
                raise ResponseChainInvalid(
                    f"SDK response chain is invalid after thread identity {thread.id!r}"
                ) from exc
            raise _classify_sdk_failure(exc, after_identity=True) from exc
        self._turns[(thread.id, turn_id)] = raw_turn
        return LiveTurnHandle(self, thread, turn_id, raw_turn)

    def live_turn(self, thread: ThreadIdentity, turn_id: str) -> LiveTurnHandle:
        """Return the exact retained handle, rejecting stale turn identities."""

        try:
            turn = self._turns[(thread.id, turn_id)]
        except KeyError as exc:
            raise StaleTurnIdentity(f"unknown live turn identity {turn_id!r}") from exc
        return LiveTurnHandle(self, thread, turn_id, turn)

    def current_turn(self, thread: ThreadIdentity) -> LiveTurnHandle | None:
        """Return the sole currently retained turn for a thread, if any."""

        matches = [(turn_id, raw) for (thread_id, turn_id), raw in self._turns.items() if thread_id == thread.id]
        if not matches:
            return None
        if len(matches) > 1:
            raise StaleTurnIdentity("multiple live turns conflict for one thread")
        turn_id, raw = matches[0]
        return LiveTurnHandle(self, thread, turn_id, raw)

    def steer(self, thread: ThreadIdentity, turn_id: str, input: str | SkillInput) -> None:
        self.live_turn(thread, turn_id).steer(input)

    def interrupt(self, thread: ThreadIdentity, turn_id: str) -> None:
        self.live_turn(thread, turn_id).interrupt()

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


class CodexSdkNotifier:
    """Resume one exact source thread through the inherited standard SDK session."""

    def __init__(
        self,
        *,
        sdk: object | None = None,
        client_factory: Callable[[], _SdkClient] | None = None,
    ) -> None:
        self._sdk = cast(_SdkSurface, sdk or _load_sdk())
        self._client_factory = client_factory or self._production_client

    def _production_client(self) -> _SdkClient:
        try:
            # No private CODEX_HOME, model, effort, cwd or config override is
            # supplied.  The official SDK owns the shared session store.
            return self._sdk.Codex()
        except (AttributeError, ImportError, TypeError, ValueError) as exc:
            raise WakeDeliveryUnavailable("shared SDK session is unavailable") from exc

    def deliver(self, source_thread_id: str, payload: str) -> str:
        if not source_thread_id or not isinstance(payload, str) or not payload:
            raise ValueError("source wake facts are incomplete")
        client: _SdkClient | None = None
        turn_started = False
        try:
            client = self._client_factory()
            thread = client.thread_resume(source_thread_id)
            turn_started = True
            raw_turn = thread.turn(payload)
            turn_id = _turn_id(raw_turn)
            return turn_id
        except WakeDeliveryUnavailable:
            raise
        except Exception as exc:
            if turn_started:
                raise WakeDeliveryAmbiguous("source wake crossed the SDK turn boundary") from exc
            raise WakeDeliveryUnavailable("source thread could not be resumed") from exc
        finally:
            if client is not None:
                client.close()


__all__ = [
    "LEAF_WORKER_ALLOWED_OPERATIONS",
    "LEAF_WORKER_CONFIG_OVERRIDES",
    "LEAF_WORKER_THREAD_CONFIG",
    "CodexSdkAdapter",
    "CodexSdkConfig",
    "CodexSdkNotifier",
    "ControllerThreadInspection",
    "ControllerThreadInspectionKind",
    "LiveTurnHandle",
    "ResponseChainInvalid",
    "SchemaOutputInvalid",
    "StaleTurnIdentity",
    "ThreadInspection",
    "ThreadInspectionKind",
    "WakeDeliveryAmbiguous",
    "WakeDeliveryUnavailable",
]
