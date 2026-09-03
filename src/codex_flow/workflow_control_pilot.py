"""One bounded real workflow-control milestone through the packaged CLI."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .contracts import ModelFacingResult, model_facing_result_schema
from .controller import capsule_json
from .domain import (
    ExecutionCapsule,
    MilestoneId,
    NativePermissionMode,
    ReasoningEffort,
    RunId,
    ValidationSpec,
    WorkspaceMode,
)
from .harness import process_birth_identity
from .ipc import IpcError, send_request
from .ledger import Ledger, LedgerError, RecordNotFound

_TERMINAL_QUEUE_STATES = frozenset({"completed", "failed", "cancelled", "human_attention_required"})
_HARNESS_READY_TIMEOUT_SECONDS = 10.0
_PILOT_TIMEOUT_SECONDS = 300.0


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_command_result(result: subprocess.CompletedProcess[str]) -> dict[str, object] | None:
    try:
        decoded = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _harness_command(root: Path) -> tuple[str, ...]:
    return ("codex-flow", "harness", "run", "--foreground", "--state-root", os.fspath(root))


def _harness_ready(root: Path, process: subprocess.Popen[str], *, timeout_seconds: float) -> None:
    """Wait only for the local harness lease/socket, never for provider state."""

    deadline = time.monotonic() + timeout_seconds
    socket_path = root / ".codex-flow" / "runtime" / "harness.sock"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("temporary harness exited before readiness")
        if socket_path.is_socket() and not socket_path.is_symlink():
            try:
                response = send_request(socket_path, {"version": 1, "operation": "status"}, timeout=0.5)
            except (IpcError, OSError):
                pass
            else:
                if response.get("version") == 1 and response.get("ok") is True:
                    return
        time.sleep(0.05)
    raise TimeoutError("temporary harness did not become ready")


def _exact_process_is_live(pid: int, birth_identity: str) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return process_birth_identity(pid) == birth_identity


def _wait_for_worker_or_terminal(
    root: Path,
    dispatch_id: str,
    process: subprocess.Popen[str],
    *,
    deadline: float,
    prior_identity: tuple[int, int, int, str] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Observe local durable lifecycle until terminal or one new exact worker."""

    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("temporary harness exited while the pilot was active")
            queue = ledger.queue_dispatch(dispatch_id)
            live = ledger.worker_liveness(dispatch_id)
            if str(queue["state"]) in _TERMINAL_QUEUE_STATES:
                return queue, live
            if live is not None and live["exited_at"] is None:
                identity = (
                    int(live["generation"]),
                    int(live["attempt"]),
                    int(live["pid"]),
                    str(live["process_birth_identity"]),
                )
                if identity != prior_identity:
                    return queue, live
            time.sleep(0.05)
    finally:
        ledger.close()
    raise TimeoutError("worker did not start or reach a durable terminal state")


def _wait_for_terminal_result(
    root: Path,
    dispatch_id: str,
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object], dict[str, object] | None, int]:
    """Wait by exact child liveness, then read the durable queue result.

    A transient recovery may create another exact attempt.  The loop observes
    only repository-local process and ledger authority; it never reads or
    polls a Codex task/thread.
    """

    deadline = time.monotonic() + timeout_seconds
    prior_identity: tuple[int, int, int, str] | None = None
    observed_attempts = 0
    last_live: dict[str, object] | None = None
    while time.monotonic() < deadline:
        queue, live = _wait_for_worker_or_terminal(
            root,
            dispatch_id,
            process,
            deadline=deadline,
            prior_identity=prior_identity,
        )
        if str(queue["state"]) in _TERMINAL_QUEUE_STATES:
            terminal_live = live or last_live
            if terminal_live is not None and terminal_live.get("exited_at") is None:
                pid = int(terminal_live["pid"])
                birth_identity = str(terminal_live["process_birth_identity"])
                while time.monotonic() < deadline and _exact_process_is_live(pid, birth_identity):
                    if process.poll() is not None:
                        raise RuntimeError("temporary harness exited before worker acknowledgement")
                    time.sleep(0.05)
                if _exact_process_is_live(pid, birth_identity):
                    raise TimeoutError("terminal worker did not exit after result acknowledgement")
                terminal_ledger = Ledger(root / ".codex-flow" / "workflow.db")
                try:
                    terminal_live = terminal_ledger.worker_liveness(dispatch_id)
                finally:
                    terminal_ledger.close()
            return queue, terminal_live, observed_attempts
        if live is None:  # pragma: no cover - guarded by helper contract
            raise RuntimeError("nonterminal dispatch has no worker identity")
        identity = (
            int(live["generation"]),
            int(live["attempt"]),
            int(live["pid"]),
            str(live["process_birth_identity"]),
        )
        prior_identity = identity
        last_live = live
        observed_attempts += 1
        while time.monotonic() < deadline and _exact_process_is_live(identity[2], identity[3]):
            if process.poll() is not None:
                raise RuntimeError("temporary harness exited while its worker was active")
            time.sleep(0.1)
    raise TimeoutError("worker did not reach a durable terminal state")


def _shutdown_harness(
    root: Path,
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 10.0,
) -> bool:
    """Stop one temporary owner through authenticated IPC, with bounded cleanup."""

    socket_path = root / ".codex-flow" / "runtime" / "harness.sock"
    if process.poll() is None and socket_path.is_socket() and not socket_path.is_symlink():
        try:
            send_request(socket_path, {"version": 1, "operation": "shutdown"}, timeout=1.0)
        except (IpcError, OSError):
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=min(grace_seconds, 2.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=min(grace_seconds, 2.0))
    return process.poll() is not None and not socket_path.exists()


def run_workflow_control_pilot(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    """Run exactly one controller CLI invocation in a disposable Git checkout.

    Evidence intentionally contains hashes, statuses, and bounded identities,
    never model prose, credentials, or raw process output.
    """

    evidence: dict[str, Any]
    root: Path
    with tempfile.TemporaryDirectory(prefix="codex-flow-workflow-control-") as directory:
        root = Path(directory) / "repository"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "codex-flow@example.test")
        _git(root, "config", "user.name", "Codex Flow Workflow Control Pilot")
        target = root / "workflow_result.txt"
        protected = root / "protected_baseline.txt"
        target.write_text("before\n", encoding="utf-8")
        protected.write_text("protected\n", encoding="utf-8")
        _git(root, "add", "workflow_result.txt", "protected_baseline.txt")
        _git(root, "commit", "-qm", "workflow control pilot base")
        base = _git(root, "rev-parse", "HEAD")
        branch = _git(root, "branch", "--show-current") or "main"
        capsule = ExecutionCapsule(
            2,
            RunId("workflow-control-pilot"),
            MilestoneId("workflow-control-validation"),
            root,
            WorkspaceMode.CURRENT_CHECKOUT,
            root,
            branch,
            base,
            "workflow-control-pilot",
            ("workflow_result.txt",),
            ("protected_baseline.txt",),
            ValidationSpec(("git", "diff", "--check"), 15),
            model,
            effort,
            (
                "In this disposable repository, write workflow_result.txt with exactly "
                "'controller-reached' followed by one newline. Do not modify any other file, "
                "commit, or alter Git metadata. Return exactly one schema-v1 ModelFacingResult "
                "with status='completed', changed_surfaces=['workflow_result.txt'], one passing "
                "validation for the exact file content, durable_status='controller_acknowledged', "
                "and next_action=null."
            ),
            model_facing_result_schema(),
            NativePermissionMode.INHERIT_NATIVE,
        )
        # Keep the input outside the disposable checkout: an untracked capsule
        # inside the workspace would correctly fail the controller's mutation
        # scope gate.
        capsule_path = Path(directory) / "workflow-control-capsule.json"
        capsule_path.write_text(
            json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["CODEX_FLOW_REAL_SDK"] = "1"
        command = (
            "codex-flow",
            "control",
            "--capsule",
            str(capsule_path),
            "--state-root",
            str(root),
            "--json",
        )
        clean_shutdown = False
        harness: subprocess.Popen[str] | None = None
        try:
            harness = subprocess.Popen(
                _harness_command(root),
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            _harness_ready(root, harness, timeout_seconds=_HARNESS_READY_TIMEOUT_SECONDS)
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            payload = _safe_command_result(completed)
            dispatch_id = payload.get("dispatch_id") if payload else None
            if completed.returncode != 0 or not isinstance(dispatch_id, str):
                raise RuntimeError("controller enqueue failed")
            queue, worker, observed_attempts = _wait_for_terminal_result(
                root,
                dispatch_id,
                harness,
                timeout_seconds=_PILOT_TIMEOUT_SECONDS,
            )
            raw_result = queue.get("raw_result_json")
            result = ModelFacingResult.from_agent_message(raw_result) if isinstance(raw_result, str) else None
            observed_edit = target.read_text(encoding="utf-8") == "controller-reached\n"
            protected_unchanged = protected.read_text(encoding="utf-8") == "protected\n"
            head_unchanged = _git(root, "rev-parse", "HEAD") == base
            changed_paths = [
                row
                for row in _git(root, "status", "--short", "--untracked-files=all").splitlines()
                if ".codex-flow/" not in row
            ]
            # ``_git`` strips the command output envelope, including the one
            # leading porcelain column when the index is clean.
            exact_changed_paths = changed_paths == ["M workflow_result.txt"]
            worker_acknowledged = (
                worker is not None
                and not _exact_process_is_live(int(worker["pid"]), str(worker["process_birth_identity"]))
                and queue.get("raw_result_sha256") is not None
            )
            status = (
                "passed"
                if queue.get("state") == "completed"
                and result is not None
                and result.status.value == "completed"
                and result.changed_surfaces == ("workflow_result.txt",)
                and observed_edit
                and protected_unchanged
                and head_unchanged
                and exact_changed_paths
                and worker_acknowledged
                else "failed"
            )
            evidence = {
                "schema": "codex-flow/h5-workflow-control-medium/v1",
                "status": status,
                "route": "workflow-control/codex-flow",
                "command": ["codex-flow", "control"],
                "model": model,
                "reasoning_effort": effort.value,
                "base_sha": base,
                "controller_exit_code": completed.returncode,
                "dispatch_id_sha256": hashlib.sha256(dispatch_id.encode("utf-8")).hexdigest(),
                "controller_status": queue.get("state"),
                "controller_checkpoint": payload.get("checkpoint") if payload else None,
                "result_status": result.status.value if result is not None else None,
                "result_durable_status": result.durable_status if result is not None else None,
                "worker_result_acknowledged": worker_acknowledged,
                "worker_attempts_observed": observed_attempts,
                "observable_edit": observed_edit,
                "protected_unchanged": protected_unchanged,
                "git_head_unchanged": head_unchanged,
                "authorized_changed_paths": changed_paths,
                "final_file_sha256": _sha256(target),
                "protected_file_sha256": _sha256(protected),
            }
        except (OSError, TimeoutError, RuntimeError, ValueError, LedgerError, RecordNotFound) as exc:
            evidence = {
                "schema": "codex-flow/h5-workflow-control-medium/v1",
                "status": "failed",
                "route": "workflow-control/codex-flow",
                "model": model,
                "reasoning_effort": effort.value,
                "error_class": type(exc).__name__,
            }
        finally:
            if harness is not None:
                clean_shutdown = _shutdown_harness(root, harness)
        # This H5 evidence field is an immutable published contract; retain
        # its historical spelling while the runtime owner is now harness.
        evidence["harness_clean_shutdown"] = clean_shutdown
    evidence["temporary_repository_removed"] = not root.exists()
    if not evidence["harness_clean_shutdown"] or not evidence["temporary_repository_removed"]:
        evidence["status"] = "failed"
    return evidence


def write_workflow_control_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["run_workflow_control_pilot", "write_workflow_control_evidence"]
