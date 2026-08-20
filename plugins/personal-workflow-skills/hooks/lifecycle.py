#!/usr/bin/env python3
"""Deterministic, retry-free lifecycle hooks for native peer handoffs.

The hook is intentionally a local state machine. It never invokes a Codex
tool: a PostToolUse warning asks the model to perform the single permitted
read-only reconciliation, and the next PreToolUse list_threads call reserves
that snapshot before it is dispatched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


STATE_VERSION = 1
STATE_FILE = "native-thread-handoff.json"
MAX_ATTEMPTS = 32
MAX_SEEN_SESSIONS = 16
MAX_TITLE = 160
MAX_TITLE_INPUT = 4096
MAX_ID = 256
MAX_MODEL = 128
MAX_THINKING = 32
MAX_PROMPT_FOR_DIGEST = 32 * 1024
MAX_DIRECTORY = 160
MAX_JSON_TEXT = 256 * 1024
MAX_CANDIDATES = 256
MAX_RETAINED_MATCHES = 16
LIST_RESPONSE_KEYSETS = frozenset(
    {
        frozenset({"threads"}),
        frozenset({"pinnedThreads", "threads"}),
        frozenset({"items"}),
        frozenset({"results"}),
    }
)
UNRESOLVED_STATUSES = frozenset({"pending", "uncertain"})
MISSING = object()


class PayloadError(ValueError):
    """The native payload is malformed or internally contradictory."""


def _bounded(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def _bounded_optional(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _valid_id(value: Any) -> str | None:
    candidate = _bounded(value, MAX_ID)
    if candidate is None or any(character.isspace() for character in candidate):
        return None
    if any(unicodedata.category(character).startswith("C") for character in candidate):
        return None
    return candidate


def _event_session_id(event: dict[str, Any]) -> str | None:
    """Return the exact bounded session identity required for state access."""

    return _bounded(event.get("session_id"), MAX_ID)


def _decode_json_string(value: Any) -> Any:
    """Decode one bounded JSON string layer without recursive parsing."""

    if not isinstance(value, str):
        return value
    if not value or len(value) > MAX_JSON_TEXT:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
        return None


def _normalise_starting_state(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        raise PayloadError("target.environment.startingState must be an object")
    state_type = _bounded(value.get("type"), 64)
    if state_type is None:
        raise PayloadError("target.environment.startingState.type must be valid")
    if state_type == "working-tree":
        if set(value) != {"type"}:
            raise PayloadError("working-tree startingState contains unsupported fields")
        return {"type": state_type}
    if state_type == "branch":
        if set(value) - {"type", "branchName", "onMissing"}:
            raise PayloadError("branch startingState contains unsupported fields")
        branch_name = _bounded(value.get("branchName"), MAX_ID)
        if branch_name is None:
            raise PayloadError("branch startingState requires branchName")
        on_missing = value.get("onMissing", MISSING)
        if on_missing is not MISSING and (
            not isinstance(on_missing, str) or on_missing not in {"error", "create-branch"}
        ):
            raise PayloadError("branch startingState.onMissing is invalid")
        state = {"type": state_type, "branch_name": branch_name}
        if on_missing is not MISSING:
            state["on_missing"] = on_missing
        return state
    raise PayloadError(f"unsupported startingState.type: {state_type}")


def _normalise_environment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError("target.environment must be an object")
    environment_type = _bounded(value.get("type"), 64)
    if environment_type is None:
        raise PayloadError("target.environment.type must be a non-empty string")
    if environment_type == "local":
        if set(value) != {"type"}:
            raise PayloadError("local environment contains unsupported fields")
        return {"type": environment_type}
    if environment_type == "worktree":
        if set(value) - {"type", "startingState"}:
            raise PayloadError("worktree environment contains unsupported fields")
        environment = {"type": environment_type}
        if "startingState" in value:
            environment["starting_state"] = _normalise_starting_state(value["startingState"])
        return environment
    raise PayloadError(f"unsupported target.environment.type: {environment_type}")


def _target_context(target: Any) -> dict[str, Any]:
    """Return the complete, bounded identity of an advertised target variant."""

    if not isinstance(target, dict):
        raise PayloadError("target must be an object")

    target_type = _bounded(target.get("type"), 64)
    if target_type is None:
        raise PayloadError("target.type must be a non-empty string")
    if target_type == "project":
        if set(target) != {"type", "projectId", "environment"}:
            raise PayloadError("project target must contain projectId and environment only")
        project_id = _bounded(target.get("projectId"), MAX_ID)
        if project_id is None:
            raise PayloadError("project target requires target.projectId")
        return {
            "type": target_type,
            "project_id": project_id,
            "environment": _normalise_environment(target["environment"]),
        }
    if target_type == "projectless":
        if set(target) - {"type", "directoryName"}:
            raise PayloadError("projectless target contains unsupported fields")
        directory = target.get("directoryName", MISSING)
        if directory is not MISSING and _bounded(directory, MAX_DIRECTORY) is None:
            raise PayloadError("projectless target directoryName is malformed")
        return {
            "type": target_type,
            "directory_name": None if directory is MISSING else directory,
        }
    if target_type == "chatgptWorkCloud":
        if set(target) - {"type", "projectId"}:
            raise PayloadError("chatgptWorkCloud target contains unsupported fields")
        project_id = target.get("projectId", MISSING)
        if project_id is not MISSING and _bounded(project_id, MAX_ID) is None:
            raise PayloadError("cloud target projectId is malformed")
        return {
            "type": target_type,
            "project_id": None if project_id is MISSING else project_id,
        }
    raise PayloadError(f"unsupported target.type: {target_type}")


def _title_identity(value: Any, *, present: bool = True) -> dict[str, Any] | None:
    if not present:
        return None
    if not isinstance(value, str) or len(value) > MAX_TITLE_INPUT:
        raise PayloadError("title must be a bounded string")
    raw = value.encode("utf-8", "surrogatepass")
    normalised = unicodedata.normalize("NFC", value).strip().encode("utf-8", "surrogatepass")
    return {
        "length": len(value),
        "digest": hashlib.sha256(raw).hexdigest(),
        "normalised_digest": hashlib.sha256(normalised).hexdigest(),
    }


def _repair_payload(tool_input: Any) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Validate create_thread and make only the safe top-level projectId repair."""

    if not isinstance(tool_input, dict):
        raise PayloadError("create_thread arguments must be an object")
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise PayloadError("create_thread.prompt must be a non-empty string")

    repaired = copy.deepcopy(tool_input)
    if "title" in repaired:
        _title_identity(repaired["title"])
    top_level_project = repaired.get("projectId", MISSING)
    target = repaired.get("target", MISSING)
    changed = False

    if top_level_project is not MISSING:
        project_id = _bounded(top_level_project, MAX_ID)
        if project_id is None:
            raise PayloadError("top-level projectId must be a non-empty string")
        if target is MISSING:
            raise PayloadError("top-level projectId cannot manufacture a project target without environment")
        elif not isinstance(target, dict):
            raise PayloadError("top-level projectId conflicts with malformed target")
        else:
            target_type = target.get("type")
            if target_type != "project":
                raise PayloadError("top-level projectId conflicts with non-project target")
            nested_project = target.get("projectId", MISSING)
            if nested_project is not MISSING:
                if _bounded(nested_project, MAX_ID) is None:
                    raise PayloadError("target.projectId must be a non-empty string")
                if nested_project != project_id:
                    raise PayloadError("top-level and target projectId values conflict")
            else:
                target["projectId"] = project_id
            del repaired["projectId"]
            changed = True

    if target is MISSING:
        raise PayloadError("create_thread.target is required")
    context = _target_context(repaired.get("target"))
    return repaired, changed, context


def _prompt_digest(prompt: str) -> str:
    bounded_prompt = prompt[:MAX_PROMPT_FOR_DIGEST].encode("utf-8", "replace")
    return hashlib.sha256(bounded_prompt).hexdigest()


def _fingerprint(tool_input: dict[str, Any], context: dict[str, Any]) -> str:
    title_identity = _title_identity(tool_input.get("title"), present="title" in tool_input)
    canonical = {
        "title": title_identity,
        "target": context,
        "model": tool_input.get("model", "")[:MAX_MODEL]
        if isinstance(tool_input.get("model"), str)
        else "",
        "thinking": tool_input.get("thinking", "")[:MAX_THINKING]
        if isinstance(tool_input.get("thinking"), str)
        else "",
        "prompt_digest": _prompt_digest(tool_input["prompt"]),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "attempts": []}


def _is_unresolved_attempt(attempt: Any) -> bool:
    return isinstance(attempt, dict) and attempt.get("status") in UNRESOLVED_STATUSES


def _is_recovery_blocking_attempt(attempt: Any) -> bool:
    return _is_unresolved_attempt(attempt) or (
        isinstance(attempt, dict) and attempt.get("blocks_create") is True
    )


def _trim_attempts(attempts: list[Any]) -> list[dict[str, Any]]:
    """Bound resolved history without evicting recovery-blocking evidence."""

    valid = [item for item in attempts if isinstance(item, dict)]
    blocking_count = sum(_is_recovery_blocking_attempt(item) for item in valid)
    if blocking_count > MAX_ATTEMPTS:
        raise RuntimeError("handoff state contains too many recovery-blocking attempts")
    if len(valid) <= MAX_ATTEMPTS:
        return valid
    retained: list[dict[str, Any]] = []
    to_drop = len(valid) - MAX_ATTEMPTS
    for item in valid:
        if to_drop and not _is_recovery_blocking_attempt(item):
            to_drop -= 1
            continue
        retained.append(item)
    if to_drop:
        raise RuntimeError("handoff state cannot safely evict attempts")
    return retained


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise RuntimeError("handoff state has an unsupported version")
    attempts = value.get("attempts")
    if not isinstance(attempts, list):
        raise RuntimeError("handoff state attempts are malformed")
    # State is deliberately bounded and contains only the fields written below.
    value["attempts"] = _trim_attempts(attempts)
    return value


def _data_path() -> Path:
    data_root = os.environ.get("PLUGIN_DATA")
    if not data_root:
        raise RuntimeError("PLUGIN_DATA is not set")
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    return root / STATE_FILE


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Use an advisory lock where available; writes remain os.replace-atomic."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            import fcntl  # type: ignore[import-not-found]

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            try:
                import fcntl  # type: ignore[import-not-found]

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        return _validate_state(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise RuntimeError("handoff state is not valid JSON") from exc


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state = _validate_state(state)
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".handoff-", suffix=".tmp", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _transaction(mutator: Any) -> Any:
    path = _data_path()
    with _state_lock(path):
        state = _read_state(path)
        result = mutator(state)
        _write_state(path, state)
        return result


def _attempt_target(attempt: dict[str, Any]) -> dict[str, Any]:
    target = attempt.get("target")
    if not isinstance(target, dict):
        return {}
    return copy.deepcopy(target)


def _unresolved_attempts(state: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in state["attempts"]
        if item.get("session_id") == session_id and item.get("status") in UNRESOLVED_STATUSES
    ]


def _new_attempt(
    tool_input: dict[str, Any],
    context: dict[str, Any],
    fingerprint: str,
    event: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    title = tool_input.get("title") if isinstance(tool_input.get("title"), str) else ""
    title_identity = _title_identity(tool_input.get("title"), present="title" in tool_input)
    return {
        "fingerprint": fingerprint,
        "title": title[:MAX_TITLE],
        "title_present": "title" in tool_input,
        "title_identity": title_identity,
        "target": context,
        "model": (tool_input.get("model") or "")[:MAX_MODEL]
        if isinstance(tool_input.get("model"), str)
        else "",
        "thinking": (tool_input.get("thinking") or "")[:MAX_THINKING]
        if isinstance(tool_input.get("thinking"), str)
        else "",
        "turn_id": _bounded_optional(event.get("turn_id"), MAX_ID),
        "session_id": session_id,
        "created_at": _now(),
        "status": "pending",
        "result_classification": "pending",
        "reconciliation_classification": None,
        "reconciliation_used": False,
        "stop_surface_used": False,
        "resume_sessions": [],
    }


def _find_attempt(
    state: dict[str, Any],
    fingerprint: str,
    *,
    session_id: str,
    statuses: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    for attempt in reversed(state["attempts"]):
        if attempt.get("session_id") != session_id:
            continue
        if attempt.get("fingerprint") != fingerprint:
            continue
        if statuses is not None and attempt.get("status") not in statuses:
            continue
        return attempt
    return None


def _allow_pre_create(event: dict[str, Any]) -> dict[str, Any]:
    repaired, changed, context = _repair_payload(event.get("tool_input"))
    session_id = _event_session_id(event)
    if session_id is None:
        return _pre_allow(
            repaired if changed else None,
            warning="session_id is absent or malformed; recovery state was not recorded",
        )
    fingerprint = _fingerprint(repaired, context)
    turn_id = _bounded_optional(event.get("turn_id"), MAX_ID)

    def mutate(state: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        if _unresolved_attempts(state, session_id) or any(
            item.get("session_id") == session_id and item.get("blocks_create") is True
            for item in state["attempts"]
        ):
            return "duplicate", None
        for attempt in reversed(state["attempts"]):
            if attempt.get("session_id") != session_id:
                continue
            if attempt.get("fingerprint") != fingerprint:
                continue
            same_turn = turn_id is not None and attempt.get("turn_id") == turn_id
            if same_turn:
                return "duplicate", None
        if sum(_is_recovery_blocking_attempt(item) for item in state["attempts"]) >= MAX_ATTEMPTS:
            return "saturated", None
        state["attempts"].append(_new_attempt(repaired, context, fingerprint, event, session_id))
        state["attempts"] = _trim_attempts(state["attempts"])
        return "allowed", repaired if changed else None

    outcome, updated = _transaction(mutate)
    if outcome == "duplicate":
        return _deny("a pending or uncertain create_thread attempt already blocks this session")
    if outcome == "saturated":
        return _deny("recovery ledger is saturated with unresolved attempts; no create was dispatched")
    if updated is not None:
        return _pre_allow(updated)
    return _pre_allow(None)


def _pre_allow(updated_input: dict[str, Any] | None, *, warning: str | None = None) -> dict[str, Any]:
    specific: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if updated_input is not None:
        specific["updatedInput"] = updated_input
    output: dict[str, Any] = {"hookSpecificOutput": specific}
    if warning is not None:
        output["systemMessage"] = f"personal-workflow-skills hook warning: {warning}. Allowing operation."
    return output


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _classify_create_response(response: Any) -> tuple[str, str, str | None]:
    response = _decode_json_string(response)
    if isinstance(response, dict) and set(response) in ({"threadId"}, {"threadId", "hostId"}):
        thread_id = _valid_id(response.get("threadId"))
        host_is_valid = "hostId" not in response or _valid_id(response.get("hostId")) is not None
        if thread_id is not None and host_is_valid:
            return "confirmed", "confirmed", thread_id
    if isinstance(response, dict) and set(response) == {"clientThreadId"}:
        client_id = _valid_id(response.get("clientThreadId"))
        if client_id is not None:
            return "queued", "queued", client_id
    return "uncertain", "error", None


def _post_create(event: dict[str, Any]) -> dict[str, Any]:
    session_id = _event_session_id(event)
    if session_id is None:
        return _warning("session_id is absent or malformed; recovery state was not inspected")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return _warning("could not identify create_thread arguments; recovery remains fail-open")
    try:
        repaired, _, context = _repair_payload(tool_input)
        fingerprint = _fingerprint(repaired, context)
    except PayloadError:
        return _warning("could not fingerprint create_thread result; recovery remains fail-open")
    status, classification, peer_id = _classify_create_response(event.get("tool_response"))

    def mutate(state: dict[str, Any]) -> bool:
        attempt = _find_attempt(state, fingerprint, session_id=session_id, statuses={"pending"})
        if attempt is None:
            return False
        attempt["status"] = status
        attempt["result_classification"] = classification
        if status == "uncertain":
            attempt["blocks_create"] = True
        if peer_id is not None:
            attempt["peer_id"] = peer_id
        attempt["reconciliation_used"] = False
        return True

    found = _transaction(mutate)
    if not found:
        return _warning("create_thread result had no matching pre-dispatch attempt")
    if status == "uncertain":
        return _post_context(
            "create_thread result classified as error/uncertain. Perform exactly one read-only "
            "list_threads reconciliation matching the exact title and target project context; "
            "never create another peer in this recovery."
        )
    if status == "queued":
        return _post_context("create_thread queued successfully; do not retry or create a duplicate.")
    return _post_context("create_thread confirmed successfully; recovery state is clear.")


def _pre_list(event: dict[str, Any]) -> dict[str, Any]:
    session_id = _event_session_id(event)
    if session_id is None:
        return _pre_allow(
            None,
            warning="session_id is absent or malformed; reconciliation state was not inspected",
        )

    def mutate(state: dict[str, Any]) -> str:
        unresolved = _unresolved_attempts(state, session_id)
        if not unresolved:
            return "ordinary"
        attempt = unresolved[-1]
        if attempt.get("reconciliation_used"):
            return "duplicate"
        attempt["reconciliation_used"] = True
        attempt["reconciliation_turn_id"] = _bounded_optional(event.get("turn_id"), MAX_ID)
        return "reserved"

    outcome = _transaction(mutate)
    if outcome == "duplicate":
        return _deny("only one list_threads reconciliation is allowed for an unresolved create")
    return _pre_allow(None)


def _candidate_items(response: Any) -> tuple[str, list[Any]]:
    """Return only a valid, bounded native list snapshot.

    Invalid/error-shaped results are deliberately distinct from a valid empty
    snapshot: only the latter may prove that a create was not found.
    """

    response = _decode_json_string(response)
    if isinstance(response, list):
        if len(response) > MAX_CANDIDATES:
            return "oversized", []
        return "valid", response
    if not isinstance(response, dict):
        return "invalid", []
    keys = frozenset(response)
    if keys not in LIST_RESPONSE_KEYSETS:
        return "invalid", []
    items: list[Any] = []
    for key in ("pinnedThreads", "threads", "items", "results"):
        if key not in response:
            continue
        value = response[key]
        if not isinstance(value, list):
            return "invalid", []
        if len(items) + len(value) > MAX_CANDIDATES:
            return "oversized", []
        items.extend(value)
    return "valid", items


def _candidate_context(candidate: dict[str, Any]) -> dict[str, Any] | None:
    target = candidate.get("target", MISSING)
    if target is not MISSING:
        if not isinstance(target, dict):
            return None
        if any(
            key in candidate
            for key in ("type", "projectId", "project_id", "environment", "directoryName")
        ):
            return None
        try:
            return _target_context(target)
        except PayloadError:
            return None

    target_type = candidate.get("type", MISSING)
    project_id = candidate.get("projectId", MISSING)
    project_id_alias = candidate.get("project_id", MISSING)
    if project_id is not MISSING and project_id_alias is not MISSING:
        return None
    if target_type is MISSING:
        return None
    if not isinstance(target_type, str):
        return None
    flattened: dict[str, Any] = {"type": target_type}
    if target_type == "project":
        if project_id is MISSING:
            project_id = project_id_alias
        if project_id is MISSING or "environment" not in candidate:
            return None
        flattened["projectId"] = project_id
        flattened["environment"] = candidate["environment"]
    elif target_type == "projectless":
        if "directoryName" in candidate:
            flattened["directoryName"] = candidate["directoryName"]
        if project_id is not MISSING or project_id_alias is not MISSING or "environment" in candidate:
            return None
    elif target_type == "chatgptWorkCloud":
        if project_id is not MISSING:
            flattened["projectId"] = project_id
        elif project_id_alias is not MISSING:
            flattened["projectId"] = project_id_alias
        if "environment" in candidate or "directoryName" in candidate:
            return None
    else:
        return None
    try:
        return _target_context(flattened)
    except PayloadError:
        return None


def _candidate_title_match(attempt: dict[str, Any], candidate: dict[str, Any]) -> str:
    expected = attempt.get("title_identity")
    if not isinstance(expected, dict):
        return "unverifiable"
    if "title" not in candidate:
        return "unverifiable"
    try:
        actual = _title_identity(candidate["title"])
    except PayloadError:
        return "unverifiable"
    if actual is None:
        return "unverifiable"
    if (
        actual.get("length") == expected.get("length")
        and actual.get("digest") == expected.get("digest")
    ):
        return "exact"
    if actual.get("normalised_digest") == expected.get("normalised_digest"):
        return "normalised"
    return "none"


def _context_matches(expected: dict[str, Any], candidate: dict[str, Any] | None) -> bool:
    return candidate is not None and candidate == expected


def _post_list(event: dict[str, Any]) -> dict[str, Any]:
    session_id = _event_session_id(event)
    if session_id is None:
        return _warning("session_id is absent or malformed; reconciliation state was not inspected")
    snapshot_classification, response_items = _candidate_items(event.get("tool_response"))

    def mutate(state: dict[str, Any]) -> tuple[str, str | None]:
        attempt = next(
            (
                item
                for item in reversed(state["attempts"])
                if (
                    item.get("session_id") == session_id
                    and item.get("status") == "uncertain"
                    and item.get("reconciliation_used")
                )
            ),
            None,
        )
        if attempt is None:
            return "ordinary", None
        if snapshot_classification != "valid":
            attempt["status"] = "ambiguous"
            attempt["blocks_create"] = True
            attempt["result_classification"] = "reconciled_ambiguous"
            attempt["reconciliation_classification"] = {
                "invalid": "invalid_snapshot",
                "oversized": "oversized_snapshot",
            }[snapshot_classification]
            return "ambiguous", None
        matches: list[str] = []
        uncertain_match_count = 0
        expected_target = _attempt_target(attempt)
        for item in response_items:
            if not isinstance(item, dict):
                uncertain_match_count = min(uncertain_match_count + 1, MAX_RETAINED_MATCHES)
                continue
            title_match = _candidate_title_match(attempt, item)
            if title_match == "none":
                continue
            candidate_context = _candidate_context(item)
            if candidate_context is None:
                uncertain_match_count = min(uncertain_match_count + 1, MAX_RETAINED_MATCHES)
                continue
            if not _context_matches(expected_target, candidate_context):
                continue
            if title_match == "exact":
                peer_id = _valid_id(item.get("threadId"))
                if peer_id is None:
                    uncertain_match_count = min(uncertain_match_count + 1, MAX_RETAINED_MATCHES)
                else:
                    if len(matches) < MAX_RETAINED_MATCHES:
                        matches.append(peer_id)
            elif title_match in {"normalised", "unverifiable"}:
                uncertain_match_count = min(uncertain_match_count + 1, MAX_RETAINED_MATCHES)
        if len(matches) == 1 and not uncertain_match_count:
            attempt["status"] = "confirmed"
            attempt["result_classification"] = "reconciled_found"
            attempt["reconciliation_classification"] = "found"
            peer_id = matches[0]
            attempt["peer_id"] = peer_id
            return "found", peer_id
        if not response_items and len(matches) == 0 and not uncertain_match_count:
            attempt["status"] = "not_found"
            attempt["result_classification"] = "reconciled_not_found"
            attempt["reconciliation_classification"] = "not_found"
            return "not_found", None
        attempt["status"] = "ambiguous"
        attempt["blocks_create"] = True
        attempt["result_classification"] = "reconciled_ambiguous"
        attempt["reconciliation_classification"] = "ambiguous"
        return "ambiguous", None

    classification, peer_id = _transaction(mutate)
    if classification == "found":
        suffix = f" ({peer_id})" if peer_id else ""
        return _post_context(
            "Reconciliation found exactly one existing peer%s. Continue with it; never create "
            "another peer."%suffix
        )
    if classification == "not_found":
        return _post_context(
            "Reconciliation returned a valid empty snapshot. No peer was found; do not retry "
            "create_thread or replace the permanently blocked attempt."
        )
    if classification == "ambiguous":
        return _post_context(
            "Reconciliation was invalid, oversized, or could not prove one exact addressable title "
            "and target identity. Stop and request manual disambiguation; do not create or replace a peer."
        )
    return {}


def _post_context(context: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def _stop(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active") is True:
        return {}
    session_id = _event_session_id(event)
    if session_id is None:
        return _warning("session_id is absent or malformed; unresolved recovery was not inspected")

    def mutate(state: dict[str, Any]) -> bool:
        unresolved = _unresolved_attempts(state, session_id)
        if not unresolved:
            return False
        attempt = unresolved[-1]
        if attempt.get("stop_surface_used"):
            return False
        attempt["stop_surface_used"] = True
        return True

    if not _transaction(mutate):
        return {}
    return {
        "decision": "block",
        "reason": (
            "An unresolved native create_thread attempt is recorded. Perform the single permitted "
            "read-only list_threads reconciliation (or report manual ambiguity); never create a "
            "continuation or duplicate peer."
        ),
    }


def _session_start(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("source") != "resume":
        return {}
    session_id = _event_session_id(event)
    if session_id is None:
        return _warning("session_id is absent or malformed; unresolved recovery was not inspected")

    def mutate(state: dict[str, Any]) -> str | None:
        unresolved = _unresolved_attempts(state, session_id)
        if not unresolved:
            return None
        attempt = unresolved[-1]
        sessions = attempt.setdefault("resume_sessions", [])
        if not isinstance(sessions, list):
            sessions = []
            attempt["resume_sessions"] = sessions
        if session_id in sessions:
            return None
        sessions.append(session_id)
        attempt["resume_sessions"] = sessions[-MAX_SEEN_SESSIONS:]
        return (
            f"Unresolved native create_thread recovery for title {attempt.get('title', '')!r}, "
            f"target project context {attempt.get('target', {})!r}, status {attempt.get('status')}. "
            "Perform at most one read-only list_threads reconciliation; do not create a replacement."
        )

    context = _transaction(mutate)
    if context is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _warning(message: str) -> dict[str, Any]:
    return {"systemMessage": f"personal-workflow-skills hook warning: {message}. Allowing operation."}


def _is_tool(tool_name: Any, expected: str) -> bool:
    return tool_name in {expected, f"codex_app__{expected}"}


def dispatch(event: dict[str, Any]) -> dict[str, Any]:
    event_name = event.get("hook_event_name")
    tool_name = event.get("tool_name")
    if event_name == "PreToolUse" and _is_tool(tool_name, "create_thread"):
        try:
            return _allow_pre_create(event)
        except PayloadError as exc:
            return _deny(str(exc))
    if event_name == "PreToolUse" and _is_tool(tool_name, "list_threads"):
        return _pre_list(event)
    if event_name == "PostToolUse" and _is_tool(tool_name, "create_thread"):
        return _post_create(event)
    if event_name == "PostToolUse" and _is_tool(tool_name, "list_threads"):
        return _post_list(event)
    if event_name == "Stop":
        return _stop(event)
    if event_name == "SessionStart":
        return _session_start(event)
    return {}


def _safe_event(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    event = _safe_event(sys.stdin.read())
    if event is None:
        print(json.dumps(_warning("input was not a JSON object"), separators=(",", ":")))
        return 0
    try:
        output = dispatch(event)
    except Exception as exc:  # hooks fail open by design
        output = _warning(f"internal {type(exc).__name__}")
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
