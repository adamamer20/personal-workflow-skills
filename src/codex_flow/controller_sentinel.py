"""Opt-in real H3 editing/resume sentinel for the canonical controller path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .controller import Controller, execution_json, git_authority_snapshot, protected_paths_digest
from .domain import (
    ExecutionCapsule,
    MilestoneId,
    ReasoningEffort,
    RunId,
    ValidationSpec,
    WorkspaceMode,
    strict_json_loads,
)
from .native_profile import NativeProfileProjection


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=path, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def run_controller_sentinel(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-flow-h3-sentinel-") as directory:
        global_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
        native_profile_before = NativeProfileProjection.load(global_home)
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
        validation_program = (
            "from pathlib import Path; "
            "assert Path('result.txt').read_text() == 'controller sentinel passed\\n'; "
            "assert Path('protected.txt').read_text() == 'must remain unchanged\\n'"
        )
        capsule = ExecutionCapsule(
            2,
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
                    validation_program,
                ),
                20,
            ),
            model,
            effort,
            (
                "Create result.txt containing exactly `controller sentinel passed` followed by one newline. "
                "Do not modify protected.txt or any other tracked file. Return only JSON matching the supplied schema; "
                "report the changed file exactly."
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
        first = Controller(repository)
        planned = first.plan(capsule)
        first.close()

        crash_output = repository.parent / "h3-crash-worker.json"
        _run_worker(repository, "crash", crash_output)
        durable_payload = strict_json_loads(crash_output.read_text())
        resume_output = repository.parent / "h3-resume-worker.json"
        _run_worker(repository, "resume", resume_output)
        terminal_payload = strict_json_loads(resume_output.read_text())

        verifier = Controller(repository)
        lease = verifier.ledger.get_workspace_lease(workspace)
        snapshot = verifier.ledger.snapshot(capsule.run_id)
        lifecycle = verifier.ledger.sdk_lifecycle_events(capsule.run_id, capsule.milestone_id)
        integrity = verifier.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        git_after = git_authority_snapshot(workspace)
        protected_after = protected_paths_digest(workspace, capsule.protected_paths)
        diff = _git(workspace, "status", "--short")
        head_before = base_sha
        head_after = _git(workspace, "rev-parse", "HEAD")
        branch_before = capsule.branch
        branch_after = _git(workspace, "branch", "--show-current")
        commit_count_before = int(_git(repository, "rev-list", "--count", "HEAD"))
        commit_count_after = int(_git(workspace, "rev-list", "--count", "HEAD"))
        commits_after = int(_git(workspace, "rev-list", "--count", f"{base_sha}..HEAD"))
        committed_paths = _git(workspace, "diff", "--name-only", base_sha).splitlines()
        changed_paths = sorted(
            set(committed_paths)
            | {
                line[3:]
                for line in diff.splitlines()
                if len(line) >= 4 and line[:2] in {"??", " M", "M ", "A ", " D", "D "}
            }
        )
        diff_stat = _git(workspace, "diff", "--stat", base_sha)
        native_profile_after = NativeProfileProjection.load(global_home)
        evidence = {
            "status": "passed" if terminal_payload["status"] == "completed" else "failed",
            "fresh_process_crash_recovery": True,
            "crash_injected_after_identity": durable_payload.get("crash_injected_after_identity", False),
            "crash_boundary": durable_payload.get("crash_boundary"),
            "planned": execution_json(planned),
            "durable_after_crash": durable_payload["record"],
            "terminal": terminal_payload,
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
            "thread_identity_reused": durable_payload["record"].get("thread_id") == terminal_payload.get("thread_id"),
            "ordered_lifecycle_events": [
                {"sequence": event.sequence, "method": event.method, "turn_id": event.turn_id} for event in lifecycle
            ],
            "protected_digest_unchanged": terminal_payload["protected_before_sha256"] == protected_after,
            "native_profile": {
                "integrity_provenance": integrity.provenance,
                "sha256": integrity.native_profile_sha256,
                "compatibility_sha256": integrity.native_compatibility_sha256,
                "effective_permission_sha256": integrity.effective_permission_sha256,
                "sanitized_effective": native_profile_before.sanitized_facts,
                "effective_permissions": (
                    integrity.effective_permission.facts
                    if integrity.effective_permission is not None
                    else native_profile_before.effective_permissions(capsule.permission_mode)
                ),
                "source_config_before_sha256": native_profile_before.config_source.sha256,
                "source_config_after_sha256": native_profile_after.config_source.sha256,
                "source_config_unchanged": (
                    native_profile_before.config_source.sha256 == native_profile_after.config_source.sha256
                ),
                "profile_unchanged": native_profile_before.profile_sha256 == native_profile_after.profile_sha256,
                "provider_route_completed": (
                    native_profile_before.provider_id == "codex-lb" and terminal_payload["status"] == "completed"
                ),
                "allowed_workspace_write_completed": (workspace / "result.txt").is_file(),
            },
            "git_authority": {
                "before_sha256": integrity.git_authority_before_sha256,
                "after_sha256": integrity.git_authority_after_sha256,
                "equal": integrity.git_authority_before_sha256 == integrity.git_authority_after_sha256,
                "verified_after": git_after.details,
                "verified_after_sha256": git_after.sha256,
            },
            "git_status": diff.splitlines(),
            "git_facts": {
                "head_before": head_before,
                "head_after": head_after,
                "branch_before": branch_before,
                "branch_after": branch_after,
                "commit_count_before": commit_count_before,
                "commit_count_after": commit_count_after,
                "commit_count_from_base": commits_after,
                "commit_created": commit_count_after > commit_count_before,
                "changed_paths": changed_paths,
                "diff_stat": diff_stat,
            },
            "result_file": (workspace / "result.txt").read_text(),
            "projection_exists_after_terminal_return": (
                repository / ".codex-flow" / "runs" / str(capsule.run_id) / "execution.json"
            ).is_file(),
        }
        verifier.close()
        if not all(
            (
                evidence["status"] == "passed",
                evidence["crash_injected_after_identity"],
                evidence["crash_boundary"] == "after_turn",
                evidence["dispatch_count"] == 1,
                evidence["thread_identity_reused"],
                evidence["protected_digest_unchanged"],
                evidence["projection_exists_after_terminal_return"],
                evidence["result_file"] == "controller sentinel passed\n",
                evidence["native_profile"]["source_config_unchanged"],
                evidence["native_profile"]["profile_unchanged"],
                evidence["native_profile"]["compatibility_sha256"] is not None,
                evidence["native_profile"]["effective_permission_sha256"] is not None,
                evidence["native_profile"]["provider_route_completed"],
                evidence["native_profile"]["allowed_workspace_write_completed"],
                evidence["git_authority"]["equal"],
                evidence["git_authority"]["after_sha256"] == evidence["git_authority"]["verified_after_sha256"],
                evidence["git_facts"]["head_before"] == evidence["git_facts"]["head_after"],
                evidence["git_facts"]["branch_before"] == evidence["git_facts"]["branch_after"],
            )
        ):
            evidence["status"] = "failed"
        return evidence


def write_controller_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _run_worker(repository: Path, phase: str, output: Path) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "codex_flow.controller_sentinel",
            "--worker",
            phase,
            "--repository",
            str(repository),
            "--output",
            str(output),
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"fresh-process controller sentinel worker failed: {detail}")


def _worker_main(phase: str, repository: Path, output: Path) -> None:
    controller = None
    try:
        controller = Controller(repository)
        if phase == "crash":

            def crash(stage: str) -> None:
                if stage == "after_turn":
                    raise RuntimeError("fresh-process post-turn crash")

            controller.close()
            controller = Controller(repository, fault_injector=crash)
            try:
                controller.start("h3-sentinel", "edit")
            except RuntimeError as exc:
                if str(exc) != "fresh-process post-turn crash":
                    raise
            record = controller.status("h3-sentinel", "edit")
            output.write_text(
                json.dumps(
                    {
                        "crash_injected_after_identity": True,
                        "crash_boundary": "after_turn",
                        "record": execution_json(record),
                    },
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
        elif phase == "resume":
            record = controller.resume("h3-sentinel", "edit")
            output.write_text(json.dumps(execution_json(record), sort_keys=True, allow_nan=False) + "\n")
        else:
            raise ValueError(f"unknown worker phase: {phase}")
    finally:
        if controller is not None:
            controller.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=("crash", "resume"))
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker is not None:
        if args.repository is None or args.output is None:
            raise SystemExit("--worker requires --repository and --output")
        _worker_main(args.worker, args.repository, args.output)


if __name__ == "__main__":
    main()
