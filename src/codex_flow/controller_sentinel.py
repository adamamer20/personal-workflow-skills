"""Opt-in real H3 editing/resume sentinel for the canonical controller path."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .controller import Controller, execution_json, protected_paths_digest
from .domain import (
    ExecutionCapsule,
    MilestoneId,
    ReasoningEffort,
    RunId,
    ValidationSpec,
    WorkspaceMode,
)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=path, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def run_controller_sentinel(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-flow-h3-sentinel-") as directory:
        repository = Path(directory) / "repository"
        repository.mkdir()
        _git(repository, "init", "-q")
        _git(repository, "config", "user.email", "codex-flow@example.test")
        _git(repository, "config", "user.name", "Codex Flow Sentinel")
        (repository / "protected.txt").write_text("must remain unchanged\n")
        (repository / "README.md").write_text("# Disposable controller sentinel\n")
        _git(repository, "add", "protected.txt", "README.md")
        _git(repository, "commit", "-qm", "sentinel base")
        base_sha = _git(repository, "rev-parse", "HEAD")
        workspace = repository.parent / "repository.worktrees" / "sdk-controller-sentinel"
        capsule = ExecutionCapsule(
            1,
            RunId("h3-sentinel"),
            MilestoneId("edit"),
            repository,
            WorkspaceMode.MANAGED_WORKTREE,
            workspace,
            "agent/sdk-controller-sentinel",
            base_sha,
            "sdk-controller-sentinel",
            ("result.txt",),
            ("protected.txt",),
            ValidationSpec(
                (
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('result.txt').read_text() == 'controller sentinel passed\\n'; "
                    "assert Path('protected.txt').read_text() == 'must remain unchanged\\n'",
                ),
                20,
            ),
            model,
            effort,
            (
                "Create exactly one file named result.txt containing exactly `controller sentinel passed` followed by "
                "one newline. Do not modify protected.txt or any other tracked file. Then return only JSON matching "
                "the supplied schema."
            ),
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "changed_file": {"type": "string"},
                },
                "required": ["status", "changed_file"],
                "additionalProperties": False,
            },
        )
        crash_seen = False

        def crash(stage: str) -> None:
            nonlocal crash_seen
            if stage == "after_turn":
                crash_seen = True
                raise RuntimeError("sentinel post-identity post-turn crash")

        first = Controller(repository, fault_injector=crash)
        planned = first.plan(capsule)
        try:
            first.start(capsule.run_id, capsule.milestone_id)
        except RuntimeError as exc:
            if str(exc) != "sentinel post-identity post-turn crash":
                raise
        durable_after_crash = first.status(capsule.run_id, capsule.milestone_id)
        first.close()

        second = Controller(repository)
        terminal = second.resume(capsule.run_id, capsule.milestone_id)
        lease = second.ledger.get_workspace_lease(workspace)
        snapshot = second.ledger.snapshot(capsule.run_id)
        lifecycle = second.ledger.sdk_lifecycle_events(capsule.run_id, capsule.milestone_id)
        protected_after = protected_paths_digest(workspace, capsule.protected_paths)
        diff = _git(workspace, "status", "--short")
        evidence = {
            "status": "passed" if terminal.status.value == "completed" else "failed",
            "crash_injected_after_identity": crash_seen,
            "planned": execution_json(planned),
            "durable_after_crash": execution_json(durable_after_crash),
            "terminal": execution_json(terminal),
            "dispatch_count": len(snapshot.dispatches),
            "workspace_lease": {
                "path": str(lease.workspace_path),
                "repository_root": str(lease.repository_root),
                "mode": lease.mode.value,
                "branch": lease.branch,
                "base_sha": lease.base_sha,
                "lane": lease.lane,
                "owner_run_id": str(lease.owner_run_id),
            },
            "thread_identity_reused": durable_after_crash.thread_id == terminal.thread_id,
            "ordered_lifecycle_events": [
                {"sequence": event.sequence, "method": event.method, "turn_id": event.turn_id} for event in lifecycle
            ],
            "protected_digest_unchanged": terminal.protected_before_sha256 == protected_after,
            "git_status": diff.splitlines(),
            "result_file": (workspace / "result.txt").read_text(),
            "projection_exists_after_result": (
                repository / ".codex-flow" / "runs" / str(capsule.run_id) / "execution.json"
            ).is_file(),
        }
        second.close()
        if not all(
            (
                evidence["status"] == "passed",
                evidence["crash_injected_after_identity"],
                evidence["dispatch_count"] == 1,
                evidence["thread_identity_reused"],
                evidence["protected_digest_unchanged"],
                evidence["projection_exists_after_result"],
                evidence["result_file"] == "controller sentinel passed\n",
            )
        ):
            evidence["status"] = "failed"
        return evidence


def write_controller_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
