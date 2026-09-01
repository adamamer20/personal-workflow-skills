"""One bounded, opt-in exact-wheel live-worker steer sentinel."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends.codex_sdk import LEAF_WORKER_CONFIG_OVERRIDES, CodexSdkAdapter, CodexSdkConfig, NativeRuntimeConfig
from .contracts import model_facing_result_schema_sha256
from .control_client import ControlClientError, LiveWorkerControlClient
from .domain import (
    NativePermissionAuthority,
    NativePermissionMode,
    ReasoningEffort,
    ThreadIdentity,
    redact_diagnostic_text,
)
from .ledger import Ledger
from .supervisor import Supervisor

_MARKER = "LIVE_CONTROL_STEER_SENTINEL"
_DISPATCH = "sentinel/live-worker-control/executor/1"


def _git_init(root: Path) -> None:
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    subprocess.run(("git", "init", "--quiet", os.fspath(root)), check=True, env=env)
    (root / "README.md").write_text("live worker sentinel\n", encoding="utf-8")


def _repository_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or ".codex-flow" in path.parts:
            continue
        snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _safe_error(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "detail": redact_diagnostic_text(" ".join(str(exc).split())[:256], limit=256)}


def run_live_control_sentinel(*, model: str, effort: ReasoningEffort, timeout_seconds: float = 90.0) -> dict[str, Any]:
    """Run exactly one provider turn through the production worker path."""

    evidence: dict[str, Any] = {
        "schema": "codex-flow/live-worker-control/v1",
        "status": "external_blocked",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sdk": {"package": "openai-codex", "version": "unknown", "runtime_version": "unknown"},
        "provider_attempts": 0,
        "worker": {"production_entrypoint": True, "leaf_worker": True, "app_api_calls": 0, "task_api_calls": 0},
        "control": {"steer_submitted": False, "marker_observed": False, "activity_observed": False},
        "repository": {"before_sha256": None, "after_sha256": None, "unchanged": False},
        "cleanup": {"temporary_repository_removed": False, "thread_archived": False},
        "failure": {
            "queue_state": None,
            "recovery_state": None,
            "exit_classification": None,
            "exit_code": None,
            "strategy": None,
            "last_failure": None,
            "detail": None,
        },
    }
    if timeout_seconds <= 0:
        raise ValueError("sentinel timeout must be positive")
    supervisor: Supervisor | None = None
    supervisor_thread: threading.Thread | None = None
    ledger: Ledger | None = None
    with tempfile.TemporaryDirectory(prefix="codex-flow-live-control-") as temporary:
        root = Path(temporary)
        _git_init(root)
        before = _repository_snapshot(root)
        evidence["repository"]["before_sha256"] = hashlib.sha256(
            json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            from .native_profile import NativeProfileProjection

            profile = NativeProfileProjection.load(
                Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve(strict=True)
            )
            authority = NativePermissionAuthority(NativePermissionMode.READ_ONLY, "read-only", "never")
            route = {
                "leaf_worker_policy": {
                    "agents.enabled": False,
                    "features.multi_agent": False,
                    "config_overrides": list(LEAF_WORKER_CONFIG_OVERRIDES),
                },
                "effective_permission": authority.facts,
                "native_profile_sha256": profile.profile_sha256,
                "native_compatibility_sha256": profile.worker_compatibility_sha256,
            }
            capsule = {
                "model": model,
                "reasoning_effort": effort.value,
                "prompt": (
                    "Wait until a live steer command arrives. Do not finish the turn before then. "
                    f"After the steer, return exactly one JSON object matching the ModelFacingResult contract, "
                    f"with summary containing {_MARKER!r}; do not use tools or modify files."
                ),
                "workspace_path": os.fspath(root),
            }
            state_dir = root / ".codex-flow"
            state_dir.mkdir(mode=0o700)
            ledger = Ledger(state_dir / "workflow.db")
            ledger.create_run("sentinel")
            ledger.create_milestone("sentinel", "live-worker-control")
            ledger.claim_dispatch("sentinel", "live-worker-control", "executor", 1)
            ledger.enqueue_dispatch(
                _DISPATCH,
                backend="sdk_headless",
                capsule_json=json.dumps(capsule, sort_keys=True, separators=(",", ":")),
                route_json=json.dumps(route, sort_keys=True, separators=(",", ":")),
                workspace_path=root,
                result_contract_sha256=model_facing_result_schema_sha256(),
                permission_mode=NativePermissionMode.READ_ONLY,
                native_profile_sha256=profile.profile_sha256,
                native_compatibility_sha256=profile.worker_compatibility_sha256,
                effective_permission=authority,
                provider_env_key=profile.provider_env_key,
                profile_sha256=profile.profile_sha256,
                checkpoint_seconds=60.0,
            )
            supervisor = Supervisor(
                root,
                lease_seconds=10.0,
                worker_command=(sys.executable, "-m", "codex_flow.worker"),
            )
            evidence["provider_attempts"] = 1
            evidence["sdk"]["version"] = "0.147.0"
            supervisor_thread = threading.Thread(
                target=supervisor.run_foreground,
                kwargs={"timeout": 0.1},
                name="codex-flow-live-control-sentinel",
                daemon=True,
            )
            supervisor_thread.start()
            client = LiveWorkerControlClient.for_state_root(root, timeout=2.0)
            deadline = time.monotonic() + timeout_seconds
            steered = False
            thread_id: str | None = None
            turn_id: str | None = None
            while time.monotonic() < deadline:
                try:
                    statuses = client.status(_DISPATCH)
                except (ControlClientError, OSError, ValueError):
                    time.sleep(0.1)
                    continue
                if statuses:
                    status = statuses[0]
                    thread_id = status.thread_id.id if status.thread_id is not None else None
                    turn_id = status.active_turn_id
                    if not steered and status.state == "running" and thread_id and turn_id:
                        client.steer(
                            _DISPATCH,
                            generation=status.generation,
                            attempt=status.attempt,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            text=f"Include the marker {_MARKER} in the final summary now.",
                            command_id="live-control-sentinel-steer",
                        )
                        evidence["control"]["steer_submitted"] = True
                        steered = True
                    if steered and status.state in {"completed", "failed", "human_attention_required"}:
                        break
                time.sleep(0.1)
            if ledger is not None:
                queue = ledger.queue_dispatch(_DISPATCH)
                recovery = ledger.recovery_state(_DISPATCH)
                retry_policy = ledger.retry_policy(_DISPATCH)
                evidence["failure"].update(
                    {
                        "queue_state": queue.get("state"),
                        "recovery_state": recovery.get("recovery_state"),
                        "exit_classification": recovery.get("worker_exit_classification"),
                        "exit_code": recovery.get("exit_code"),
                        "strategy": retry_policy.get("strategy"),
                        "last_failure": retry_policy.get("last_failure"),
                    }
                )
                raw = queue.get("raw_result_json")
                if queue.get("state") == "completed" and isinstance(raw, str):
                    evidence["control"]["marker_observed"] = _MARKER in raw
                activity = ledger.recent_activity(_DISPATCH)
                evidence["control"]["activity_observed"] = any(
                    isinstance(item.get("text"), str) and _MARKER in str(item["text"]) for item in activity
                )
            evidence["status"] = (
                "passed"
                if evidence["control"]["steer_submitted"]
                and evidence["control"]["marker_observed"]
                and evidence["control"]["activity_observed"]
                else "failed"
            )
            if evidence["status"] == "passed" and thread_id is not None:
                archive_config = CodexSdkConfig(
                    model,
                    effort,
                    sandbox=None,
                    cwd=root,
                    native_runtime=NativeRuntimeConfig.shared(profile),
                    permission_mode=NativePermissionMode.READ_ONLY,
                    effective_permission=authority,
                    leaf_worker=True,
                )
                with CodexSdkAdapter(archive_config) as archive_adapter:
                    archive_adapter.archive_persisted_thread(ThreadIdentity(thread_id))
                evidence["cleanup"]["thread_archived"] = True
        except (OSError, RuntimeError, ValueError, ControlClientError) as exc:
            evidence["status"] = "failed"
            evidence["error"] = _safe_error(exc)
            evidence["failure"]["detail"] = evidence["error"]["detail"]
        finally:
            if supervisor is not None:
                supervisor._stop = True
            if supervisor_thread is not None:
                supervisor_thread.join(timeout=5.0)
            after = _repository_snapshot(root)
            evidence["repository"]["after_sha256"] = hashlib.sha256(
                json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            evidence["repository"]["unchanged"] = before == after
            if ledger is not None:
                ledger.close()
        # TemporaryDirectory removes its root only after this context exits;
        # record that fact below, outside the context, rather than observing
        # the still-live path during worker shutdown.
        evidence["cleanup"]["temporary_repository_removed"] = False
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence["cleanup"]["temporary_repository_removed"] = not root.exists()
    if evidence["status"] == "passed" and not evidence["repository"]["unchanged"]:
        evidence["status"] = "failed"
    return evidence


def write_live_control_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


__all__ = ["run_live_control_sentinel", "write_live_control_evidence"]
