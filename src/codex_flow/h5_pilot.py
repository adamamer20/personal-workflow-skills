"""One bounded real medium milestone through the packaged controller CLI."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

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

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "changed_file": {"type": "string"}},
    "required": ["status", "changed_file"],
    "additionalProperties": False,
}


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


def run_h5_medium_pilot(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    """Run exactly one controller CLI invocation in a disposable Git checkout.

    Evidence intentionally contains hashes, statuses, and bounded identities,
    never model prose, credentials, or raw process output.
    """

    with tempfile.TemporaryDirectory(prefix="codex-flow-h5-pilot-") as directory:
        root = Path(directory) / "repository"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "codex-flow@example.test")
        _git(root, "config", "user.name", "Codex Flow H5 Pilot")
        target = root / "medium.txt"
        protected = root / "protected.txt"
        target.write_text("before\n", encoding="utf-8")
        protected.write_text("protected\n", encoding="utf-8")
        _git(root, "add", "medium.txt", "protected.txt")
        _git(root, "commit", "-qm", "h5 pilot base")
        base = _git(root, "rev-parse", "HEAD")
        branch = _git(root, "branch", "--show-current") or "main"
        capsule = ExecutionCapsule(
            2,
            RunId("h5-pilot"),
            MilestoneId("medium"),
            root,
            WorkspaceMode.CURRENT_CHECKOUT,
            root,
            branch,
            base,
            "h5-pilot",
            ("medium.txt",),
            ("protected.txt",),
            ValidationSpec(("git", "diff", "--check"), 15),
            model,
            effort,
            (
                "In this disposable repository, write medium.txt with exactly "
                "'controller-reached' followed by one newline. Do not modify any "
                "other file. Return status='completed' and changed_file='medium.txt'."
            ),
            _OUTPUT_SCHEMA,
            NativePermissionMode.INHERIT_NATIVE,
        )
        # Keep the input outside the disposable checkout: an untracked capsule
        # inside the workspace would correctly fail the controller's mutation
        # scope gate.
        capsule_path = Path(directory) / "h5-capsule.json"
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
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "schema": "codex-flow/h5-workflow-control-medium/v1",
                "status": "external_blocked",
                "route": "workflow-control/codex-flow",
                "model": model,
                "reasoning_effort": effort.value,
                "error_class": type(exc).__name__,
            }
        payload = _safe_command_result(completed)
        observed_edit = target.read_text(encoding="utf-8") == "controller-reached\n"
        protected_unchanged = protected.read_text(encoding="utf-8") == "protected\n"
        changed_paths = _git(root, "status", "--short").splitlines()
        result_payload = payload.get("result") if payload else None
        result_status = result_payload.get("status") if isinstance(result_payload, dict) else None
        status = "passed" if completed.returncode == 0 and observed_edit and protected_unchanged else "failed"
        return {
            "schema": "codex-flow/h5-workflow-control-medium/v1",
            "status": status,
            "route": "workflow-control/codex-flow",
            "command": ["codex-flow", "control"],
            "model": model,
            "reasoning_effort": effort.value,
            "base_sha": base,
            "controller_exit_code": completed.returncode,
            "controller_status": payload.get("status") if payload else None,
            "controller_checkpoint": payload.get("checkpoint") if payload else None,
            "result_status": result_status,
            "observable_edit": observed_edit,
            "protected_unchanged": protected_unchanged,
            "authorized_changed_paths": changed_paths,
            "final_file_sha256": _sha256(target),
            "protected_file_sha256": _sha256(protected),
        }


def write_h5_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
