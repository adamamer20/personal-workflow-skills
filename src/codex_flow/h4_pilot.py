"""One-shot real SDK H4-A disposable objective pilot."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from .controller import Controller
from .domain import (
    Budget,
    CodexFlowError,
    DispatchId,
    FindingCausalClass,
    ReasoningEffort,
    RepairRecord,
    ReviewFinding,
    ReviewResult,
    RoleId,
    Sandbox,
    Severity,
)

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "changed_file": {"type": "string"},
    },
    "required": ["status", "changed_file"],
    "additionalProperties": False,
}


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", *arguments), cwd=path, text=True, capture_output=True, check=True)
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_h4_objective_pilot(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    """Run exactly one bounded SDK thread in a disposable repository.

    Returned evidence is sanitized: it contains hashes, classifications, and
    counts, never model prose, credentials, or raw SDK payloads.
    """

    with tempfile.TemporaryDirectory(prefix="codex-flow-h4-pilot-") as directory:
        root = Path(directory) / "repository"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "codex-flow@example.test")
        _git(root, "config", "user.name", "Codex Flow H4 Pilot")
        target = root / "objective.txt"
        target.write_text("before\n", encoding="utf-8")
        _git(root, "add", "objective.txt")
        _git(root, "commit", "-qm", "pilot base")
        base = _git(root, "rev-parse", "HEAD")
        controller = Controller(root)
        ledger = controller.ledger
        ledger.create_run("h4-pilot")
        ledger.create_milestone("h4-pilot", "objective")
        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                model=model,
                reasoning_effort=effort,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=root,
            )
        )
        thread = None
        turns = 0
        try:
            thread = adapter.start_thread()

            def objective() -> dict[str, object]:
                nonlocal turns
                observation = adapter.run_turn(
                    thread,
                    "In this disposable repository, write objective.txt with exactly 'needs-repair' followed by one newline. "
                    "Do not modify any other file. Return the requested JSON object.",
                    output_schema=_OUTPUT_SCHEMA,
                )
                turns += 1
                if observation.structured_output is None:
                    raise RuntimeError("SDK objective turn returned no structured output")
                return {"changed_file": "objective.txt", "turn_status": observation.status}

            def revision() -> str:
                return _sha256(target)

            def reviewer(revision_token: str, fresh: bool) -> ReviewResult:
                content = target.read_text(encoding="utf-8")
                if content != "repaired\n":
                    finding = ReviewFinding(
                        "F-objective-marker",
                        FindingCausalClass.IMPLEMENTATION,
                        Severity.P1,
                        True,
                        "objective marker must be repaired before successor promotion",
                        {"path": "objective.txt", "content_sha256": hashlib.sha256(content.encode()).hexdigest()},
                        "objective.txt contains the repaired marker",
                    )
                    return ReviewResult("review-1", RoleId("code-reviewer"), False, (finding,), revision_token)
                return ReviewResult(
                    "review-2",
                    RoleId("code-reviewer"),
                    True,
                    (),
                    revision_token,
                    fresh=fresh,
                    prior_review_id="review-1",
                )

            def repair(finding: ReviewFinding) -> RepairRecord:
                nonlocal turns
                observation = adapter.run_turn(
                    thread,
                    "Repair the exact prior finding in this disposable repository: write objective.txt with exactly "
                    "'repaired' followed by one newline. Do not modify any other file. Return the requested JSON object.",
                    output_schema=_OUTPUT_SCHEMA,
                )
                turns += 1
                if observation.structured_output is None:
                    raise RuntimeError("SDK repair turn returned no structured output")
                return RepairRecord(
                    "repair-1",
                    finding.finding_id,
                    DispatchId("h4-pilot/objective/executor/1"),
                    "repair objective marker through the same SDK thread",
                    "repaired",
                )

            result = controller.h4_walking_skeleton().run(
                "h4-pilot",
                "objective",
                objective=objective,
                reviewer=reviewer,
                repair=repair,
                current_revision=revision,
                budget=Budget(max_turns=2, max_repairs=1, max_reviews=2, max_validations=2),
            )
            status = "passed" if result.accepted and target.read_text(encoding="utf-8") == "repaired\n" else "failed"
            return {
                "status": status,
                "provider_route": "codex-lb",
                "model": model,
                "reasoning_effort": effort.value,
                "base_sha": base,
                "thread_started": thread is not None,
                "thread_identity_reused": True,
                "sdk_turns": turns,
                "observable_edit": target.read_text(encoding="utf-8") == "repaired\n",
                "authorized_changed_paths": _git(root, "status", "--short").splitlines(),
                "final_file_sha256": _sha256(target),
                "lifecycle_kinds": [record.kind for record in ledger.h4_lifecycle("h4-pilot", "objective")],
                "review_count": len(result.review_ids),
                "repair_count": len(result.repair_ids),
                "same_owner_repair": True,
                "accepted_state": ledger.current_state("h4-pilot", "objective").value,
            }
        except CodexFlowError as exc:
            return {
                "status": "external_blocked",
                "provider_route": "codex-lb",
                "model": model,
                "reasoning_effort": effort.value,
                "thread_started": thread is not None,
                "thread_identity_reused": True,
                "sdk_turns": turns,
                "error_class": type(exc).__name__,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "provider_route": "codex-lb",
                "model": model,
                "reasoning_effort": effort.value,
                "thread_started": thread is not None,
                "thread_identity_reused": True,
                "sdk_turns": turns,
                "error_class": type(exc).__name__,
            }
        finally:
            if thread is not None:
                try:
                    adapter.archive_thread(thread)
                except Exception:
                    pass
            adapter.close()
            controller.close()
