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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


STATE_VERSION = 1
STATE_FILE = "native-thread-handoff.json"
MAX_ATTEMPTS = 32
MAX_SEEN_SESSIONS = 16
MAX_TITLE = 160
MAX_ID = 256
MAX_MODEL = 128
MAX_THINKING = 32
MAX_PROMPT_FOR_DIGEST = 32 * 1024
MAX_DIRECTORY = 160
MAX_JSON_TEXT = 256 * 1024
UNRESOLVED_STATUSES = frozenset({"pending", "uncertain", "ambiguous"})
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
    return _bounded(value, MAX_ID)


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


def _normalise_environment(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PayloadError("target.environment must be an object")
    environment_type = _bounded(value.get("type"), 64)
    if environment_type is None:
        raise PayloadError("target.environment.type must be a non-empty string")
    return environment_type


def _target_context(target: Any) -> dict[str, str | None]:
    """Return only bounded, non-sensitive target identity fields."""

    if target is None:
        return {"type": "projectless", "project_id": None, "environment": None}
    if not isinstance(target, dict):
        raise PayloadError("target must be an object")

    target_type = _bounded(target.get("type"), 64)
    if target_type is None:
        raise PayloadError("target.type must be a non-empty string")
    environment = _normalise_environment(target.get("environment"))

    if target_type == "project":
        project_id = _bounded(target.get("projectId"), MAX_ID)
        if project_id is None:
            raise PayloadError("project target requires target.projectId")
        return {
            "type": target_type,
            "project_id": project_id,
            "environment": environment,
        }
    if target_type == "projectless":
        directory = target.get("directoryName")
        if directory is not None and _bounded(directory, MAX_DIRECTORY) is None:
            raise PayloadError("projectless target directoryName is malformed")
        return {"type": target_type, "project_id": None, "environment": environment}
    if target_type == "chatgptWorkCloud":
        project_id = target.get("projectId")
        if project_id is not None and _bounded(project_id, MAX_ID) is None:
            raise PayloadError("cloud target projectId is malformed")
        return {
            "type": target_type,
            "project_id": project_id,
            "environment": environment,
        }
    raise PayloadError(f"unsupported target.type: {target_type}")


def _repair_payload(tool_input: Any) -> tuple[dict[str, Any], bool, dict[str, str | None]]:
    """Validate create_thread and make only the safe top-level projectId repair."""

    if not isinstance(tool_input, dict):
        raise PayloadError("create_thread arguments must be an object")
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise PayloadError("create_thread.prompt must be a non-empty string")

    repaired = copy.deepcopy(tool_input)
    top_level_project = repaired.get("projectId", MISSING)
    target = repaired.get("target", MISSING)
    changed = False

    if top_level_project is not MISSING:
        project_id = _bounded(top_level_project, MAX_ID)
        if project_id is None:
            raise PayloadError("top-level projectId must be a non-empty string")
        if target is MISSING:
            repaired["target"] = {"type": "project", "projectId": project_id}
            del repaired["projectId"]
            target = repaired["target"]
            changed = True
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


def _fingerprint(tool_input: dict[str, Any], context: dict[str, str | None]) -> str:
    canonical = {
        "title": tool_input.get("title", "")[:MAX_TITLE]
        if isinstance(tool_input.get("title"), str)
        else "",
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


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise RuntimeError("handoff state has an unsupported version")
    attempts = value.get("attempts")
    if not isinstance(attempts, list):
        raise RuntimeError("handoff state attempts are malformed")
    # State is deliberately bounded and contains only the fields written below.
    value["attempts"] = [item for item in attempts if isinstance(item, dict)][-MAX_ATTEMPTS:]
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


def _attempt_target(attempt: dict[str, Any]) -> dict[str, str | None]:
    target = attempt.get("target")
    if not isinstance(target, dict):
        return {"type": "unknown", "project_id": None, "environment": None}
    return {
        "type": target.get("type"),
        "project_id": target.get("project_id"),
        "environment": target.get("environment"),
    }


def _unresolved_attempts(state: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in state["attempts"]
        if item.get("session_id") == session_id and item.get("status") in UNRESOLVED_STATUSES
    ]


def _new_attempt(
    tool_input: dict[str, Any],
    context: dict[str, str | None],
    fingerprint: str,
    event: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    title = tool_input.get("title") if isinstance(tool_input.get("title"), str) else ""
    return {
        "fingerprint": fingerprint,
        "title": title[:MAX_TITLE],
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
        for attempt in reversed(state["attempts"]):
            if attempt.get("session_id") != session_id:
                continue
            if attempt.get("fingerprint") != fingerprint:
                continue
            same_turn = turn_id is not None and attempt.get("turn_id") == turn_id
            if same_turn or attempt.get("status") in UNRESOLVED_STATUSES:
                return "duplicate", None
        state["attempts"].append(_new_attempt(repaired, context, fingerprint, event, session_id))
        state["attempts"] = state["attempts"][-MAX_ATTEMPTS:]
        return "allowed", repaired if changed else None

    outcome, updated = _transaction(mutate)
    if outcome == "duplicate":
        return _deny("same-turn or unresolved duplicate create_thread attempt")
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
    if isinstance(response, dict):
        thread_id = _valid_id(response.get("threadId"))
        if thread_id is not None:
            return "confirmed", "confirmed", thread_id
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


def _candidate_items(response: Any) -> list[Any]:
    response = _decode_json_string(response)
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []
    items: list[Any] = []
    for key in ("pinnedThreads", "threads", "items", "results"):
        value = response.get(key)
        if isinstance(value, list):
            items.extend(value)
    return items


def _candidate_context(candidate: dict[str, Any]) -> dict[str, str | None] | None:
    target = candidate.get("target")
    if isinstance(target, dict):
        try:
            return _target_context(target)
        except PayloadError:
            return None
    project_id = candidate.get("projectId")
    if project_id is None:
        project_id = candidate.get("project_id")
    if project_id is not None:
        project_id = _bounded(project_id, MAX_ID)
        if project_id is None:
            return None
        environment = candidate.get("environment")
        if isinstance(environment, dict):
            try:
                environment_type = _normalise_environment(environment)
            except PayloadError:
                return None
        else:
            environment_type = None
        return {"type": "project", "project_id": project_id, "environment": environment_type}
    if candidate.get("type") == "projectless":
        return {"type": "projectless", "project_id": None, "environment": None}
    return None


def _context_matches(
    expected: dict[str, str | None], candidate: dict[str, str | None] | None
) -> bool:
    if candidate is None:
        return False
    if candidate.get("type") != expected.get("type"):
        return False
    if candidate.get("project_id") != expected.get("project_id"):
        return False
    # list_threads summaries may omit environment while still exposing the
    # exact saved project. If it is present, it must agree exactly.
    candidate_environment = candidate.get("environment")
    return candidate_environment is None or candidate_environment == expected.get("environment")


def _post_list(event: dict[str, Any]) -> dict[str, Any]:
    session_id = _event_session_id(event)
    if session_id is None:
        return _warning("session_id is absent or malformed; reconciliation state was not inspected")
    response_items = _candidate_items(event.get("tool_response"))

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
        matches: list[dict[str, Any]] = []
        expected_title = attempt.get("title", "")
        expected_target = _attempt_target(attempt)
        for item in response_items:
            if not isinstance(item, dict):
                continue
            candidate_title = item.get("title")
            if not isinstance(candidate_title, str) or candidate_title[:MAX_TITLE] != expected_title:
                continue
            if not _context_matches(expected_target, _candidate_context(item)):
                continue
            matches.append(item)
        if len(matches) == 1:
            attempt["status"] = "confirmed"
            attempt["result_classification"] = "reconciled_found"
            attempt["reconciliation_classification"] = "found"
            peer_id = _valid_id(matches[0].get("threadId")) or _valid_id(matches[0].get("id"))
            if peer_id is not None:
                attempt["peer_id"] = peer_id
            return "found", peer_id
        if len(matches) == 0:
            attempt["status"] = "not_found"
            attempt["result_classification"] = "reconciled_not_found"
            attempt["reconciliation_classification"] = "not_found"
            return "not_found", None
        attempt["status"] = "ambiguous"
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
            "Reconciliation found no exact title and target-project match. Do not retry create_thread "
            "in this turn; a new START requires an explicit request."
        )
    if classification == "ambiguous":
        return _post_context(
            "Reconciliation found multiple exact title and target-project matches. Stop and request "
            "manual disambiguation; do not create or replace a peer."
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
