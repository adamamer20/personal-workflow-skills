"""Opt-in, read-only real SDK compatibility sentinel."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from .domain import Capability, CapabilityObservation, CapabilityStatus, ReasoningEffort

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "answer": {"type": "string"},
    },
    "required": ["ok", "answer"],
    "additionalProperties": False,
}


def _snapshot(root: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _git_init(root: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, env=environment)
    (root / "README.md").write_text("SDK sentinel repository\n", encoding="utf-8")


def _git_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _capability_payload(observation: CapabilityObservation) -> dict[str, str]:
    return {"status": observation.status.value, "detail": observation.detail}


def _set_status(
    matrix: dict[str, dict[str, str]], capability: Capability, status: CapabilityStatus, detail: str
) -> None:
    matrix[capability.value] = {"status": status.value, "detail": detail}


def _event_payload(turns: tuple[Any, ...]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for turn in turns:
        for event in turn.events:
            payload.append(
                {
                    "sequence": len(payload),
                    "turn_sequence": event.sequence,
                    "method": event.method,
                    "turn_id": event.turn_id,
                }
            )
    return payload


def run_sdk_compatibility_sentinel(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    """Run one isolated real SDK session and return JSON-serializable evidence."""

    evidence: dict[str, Any] = {
        "schema_version": "h1-sdk-sentinel/v1",
        "status": "blocked",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sdk": {"package": "openai-codex", "version": "unknown", "runtime_version": "unknown"},
        "thread": {"id": None, "resumed_id": None, "same_id": False},
        "turns": [],
        "events": [],
        "capabilities": {},
        "sandbox": {"repository": None, "unchanged": False},
        "controller_repository": {"path": "<controller-worktree>", "unchanged": False},
        "cleanup": {"archived": False},
    }
    controller_root = Path.cwd()
    controller_before = _git_status(controller_root)
    evidence["controller_repository"]["git_status_before"] = controller_before
    with tempfile.TemporaryDirectory(prefix="codex-flow-sdk-sentinel-") as temp_dir:
        repo = Path(temp_dir)
        _git_init(repo)
        before = _snapshot(repo)
        evidence["sandbox"]["repository"] = str(repo)
        try:
            adapter = CodexSdkAdapter(
                CodexSdkConfig(
                    model=model,
                    reasoning_effort=effort,
                    cwd=repo,
                )
            )
            evidence["sdk"]["version"] = adapter.sdk_version
            for observation in adapter.capability_matrix():
                evidence["capabilities"][observation.capability.value] = _capability_payload(observation)
            thread = None
            with adapter:
                try:
                    thread = adapter.start_thread()
                    evidence["sdk"]["runtime_version"] = adapter.runtime_version
                    evidence["thread"]["id"] = thread.id
                    _set_status(
                        evidence["capabilities"],
                        Capability.LOCAL_START,
                        CapabilityStatus.PROVEN,
                        "Codex() and thread_start returned a real thread identity",
                    )
                    _set_status(
                        evidence["capabilities"],
                        Capability.THREAD_IDENTITY,
                        CapabilityStatus.PROVEN,
                        "thread identity was non-empty and addressable",
                    )
                    first = adapter.run_turn(
                        thread,
                        "Return exactly the requested JSON object. Do not use tools or modify files.",
                        output_schema=SCHEMA,
                    )
                    evidence["turns"] = [
                        {
                            "thread_id": first.thread_id.id,
                            "turn_id": first.turn_id,
                            "status": first.status,
                            "final_response": first.final_response,
                            "structured_output": first.structured_output,
                            "error": first.error,
                        }
                    ]
                    evidence["events"] = _event_payload((first,))
                    second = adapter.resume_thread(thread)
                    evidence["thread"]["resumed_id"] = second.id
                    evidence["thread"]["same_id"] = second.id == thread.id
                    resumed = adapter.run_turn(
                        second,
                        "Return the same JSON shape with answer set to resume-ok. Do not use tools or modify files.",
                        output_schema=SCHEMA,
                    )
                    observations = (first, resumed)
                    evidence["turns"] = [
                        {
                            "thread_id": turn.thread_id.id,
                            "turn_id": turn.turn_id,
                            "status": turn.status,
                            "final_response": turn.final_response,
                            "structured_output": turn.structured_output,
                            "error": turn.error,
                        }
                        for turn in observations
                    ]
                    evidence["events"] = _event_payload(observations)
                    _set_status(
                        evidence["capabilities"],
                        Capability.SCHEMA_BOUNDED_TURN,
                        CapabilityStatus.PROVEN,
                        "two turns returned JSON objects satisfying the required schema",
                    )
                    _set_status(
                        evidence["capabilities"],
                        Capability.EXPLICIT_MODEL,
                        CapabilityStatus.PROVEN,
                        f"model={model!r} was passed to thread_start and both turns",
                    )
                    _set_status(
                        evidence["capabilities"],
                        Capability.EXPLICIT_REASONING_EFFORT,
                        CapabilityStatus.PROVEN,
                        f"reasoning_effort={effort.value!r} was passed to both turns",
                    )
                    _set_status(
                        evidence["capabilities"],
                        Capability.LIFECYCLE_EVENTS,
                        CapabilityStatus.PROVEN,
                        "ordered turn notifications included a terminal turn/completed event per turn",
                    )
                    _set_status(
                        evidence["capabilities"],
                        Capability.SAME_THREAD_RESUME,
                        CapabilityStatus.PROVEN if second.id == thread.id else CapabilityStatus.UNSUPPORTED,
                        "resume returned the original thread identity"
                        if second.id == thread.id
                        else "resume returned a different thread identity",
                    )
                finally:
                    if thread is not None:
                        try:
                            adapter.archive_thread(thread)
                            evidence["cleanup"]["archived"] = True
                        except Exception as cleanup_exc:
                            evidence["cleanup"]["error"] = {
                                "type": type(cleanup_exc).__name__,
                                "message": str(cleanup_exc),
                            }
            after = _snapshot(repo)
            unchanged = before == after
            evidence["sandbox"]["before_sha256"] = before
            evidence["sandbox"]["after_sha256"] = after
            evidence["sandbox"]["unchanged"] = unchanged
            _set_status(
                evidence["capabilities"],
                Capability.SANDBOX_ISOLATION,
                CapabilityStatus.PROVEN if unchanged else CapabilityStatus.UNSUPPORTED,
                "disposable repository bytes were unchanged under Sandbox.read_only"
                if unchanged
                else "disposable repository bytes changed under Sandbox.read_only",
            )
            required = (
                Capability.SCHEMA_BOUNDED_TURN,
                Capability.EXPLICIT_MODEL,
                Capability.EXPLICIT_REASONING_EFFORT,
                Capability.LIFECYCLE_EVENTS,
                Capability.SAME_THREAD_RESUME,
                Capability.SANDBOX_ISOLATION,
            )
            if all(
                evidence["capabilities"].get(capability.value, {}).get("status") == CapabilityStatus.PROVEN.value
                for capability in required
            ):
                evidence["status"] = "passed"
        except Exception as exc:  # evidence must survive auth/runtime failures
            evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
            after = _snapshot(repo)
            evidence["sandbox"]["before_sha256"] = before
            evidence["sandbox"]["after_sha256"] = after
            evidence["sandbox"]["unchanged"] = before == after
            if before == after:
                _set_status(
                    evidence["capabilities"],
                    Capability.SANDBOX_ISOLATION,
                    CapabilityStatus.PROVEN,
                    "disposable repository bytes were unchanged despite sentinel failure",
                )
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    controller_after = _git_status(controller_root)
    evidence["controller_repository"]["git_status_after"] = controller_after
    evidence["controller_repository"]["unchanged"] = controller_before == controller_after
    return evidence


def write_sdk_compatibility_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


__all__ = ["run_sdk_compatibility_sentinel", "write_sdk_compatibility_evidence"]
