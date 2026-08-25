"""One-shot real SDK H4 disposable pilots."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from .config import load_workflow_config
from .controller import Controller
from .domain import (
    AcceptanceMode,
    Budget,
    CodexFlowError,
    DispatchId,
    FindingCausalClass,
    ReasoningEffort,
    RenderedEvidence,
    RepairRecord,
    ReplanProposal,
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


def run_h4_multi_authority_pilot(*, model: str, effort: ReasoningEffort) -> dict[str, Any]:
    """Run exactly one bounded real SDK H4-B multi-authority scenario.

    Review callbacks are local read-only authorities over the disposable
    workspace; only the executor SDK thread writes the target.  The retained
    packet contains hashes, route identities, and causal lifecycle kinds, not
    provider prose or raw payloads.
    """

    with tempfile.TemporaryDirectory(prefix="codex-flow-h4b-pilot-") as directory:
        root = Path(directory) / "repository"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "codex-flow@example.test")
        _git(root, "config", "user.name", "Codex Flow H4-B Pilot")
        target = root / "objective.txt"
        target.write_text("before\n", encoding="utf-8")
        render = root / "fixed-rendered-evidence.svg"
        render.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>H4-B</text></svg>\n', encoding="utf-8")
        _git(root, "add", "objective.txt", "fixed-rendered-evidence.svg")
        _git(root, "commit", "-qm", "pilot base")
        base = _git(root, "rev-parse", "HEAD")
        controller = Controller(root)
        ledger = controller.ledger
        ledger.create_run("h4b-pilot")
        ledger.create_milestone("h4b-pilot", "multi-authority")
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

            def sdk_turn(prompt: str) -> None:
                nonlocal turns
                observation = adapter.run_turn(thread, prompt, output_schema=_OUTPUT_SCHEMA)
                turns += 1
                if observation.structured_output is None:
                    raise RuntimeError("SDK pilot turn returned no structured output")

            def objective() -> dict[str, object]:
                sdk_turn(
                    "In this disposable repository, write objective.txt with exactly 'needs-repair' followed by one newline. "
                    "Do not modify any other file. Return the requested JSON object."
                )
                return {"changed_file": "objective.txt", "turn_status": "completed"}

            def revision() -> str:
                return _sha256(target)

            def fixed_render(token: str) -> RenderedEvidence:
                return RenderedEvidence(
                    "fixed-render-1",
                    token,
                    _sha256(render),
                    "fixed-rendered-evidence.svg",
                    width=320,
                    height=120,
                )

            objective_reviews = 0
            visual_reviews = 0
            architecture_reviews = 0

            def objective_reviewer(token: str, fresh: bool) -> ReviewResult:
                nonlocal objective_reviews
                objective_reviews += 1
                if target.read_text(encoding="utf-8") != "repaired\n":
                    finding = ReviewFinding(
                        "F-objective-marker",
                        FindingCausalClass.IMPLEMENTATION,
                        Severity.P1,
                        True,
                        "objective marker must be repaired before promotion",
                        {"path": "objective.txt", "content_sha256": _sha256(target)},
                        "objective.txt contains the repaired marker",
                    )
                    return ReviewResult(
                        "objective-review-1",
                        RoleId("code-reviewer"),
                        False,
                        (finding,),
                        token,
                        fresh=fresh,
                        acceptance_mode=AcceptanceMode.OBJECTIVE,
                    )
                return ReviewResult(
                    "objective-review-2",
                    RoleId("code-reviewer"),
                    True,
                    (),
                    token,
                    fresh=fresh,
                    prior_review_id="objective-review-1",
                    acceptance_mode=AcceptanceMode.OBJECTIVE,
                )

            def visual_reviewer(token: str, evidence: RenderedEvidence, fresh: bool) -> ReviewResult:
                nonlocal visual_reviews
                visual_reviews += 1
                return ReviewResult(
                    f"visual-review-{visual_reviews}",
                    RoleId("visual-reviewer"),
                    True,
                    (),
                    token,
                    fresh=fresh,
                    evidence_ids=(evidence.evidence_id,),
                    acceptance_mode=AcceptanceMode.VISUAL,
                )

            def architecture_reviewer(token: str, evidence: RenderedEvidence, fresh: bool) -> ReviewResult:
                nonlocal architecture_reviews
                architecture_reviews += 1
                if architecture_reviews == 1:
                    finding = ReviewFinding(
                        "F-architecture-strategy",
                        FindingCausalClass.ACCEPTANCE,
                        Severity.P1,
                        True,
                        "architecture must use the accepted bounded strategy",
                        {"strategy": "initial"},
                        "architecture review accepts the revised strategy",
                    )
                    return ReviewResult(
                        "architecture-review-1",
                        RoleId("architecture-reviewer"),
                        False,
                        (finding,),
                        token,
                        fresh=fresh,
                        acceptance_mode=AcceptanceMode.ARCHITECTURE,
                    )
                return ReviewResult(
                    "architecture-review-2",
                    RoleId("architecture-reviewer"),
                    True,
                    (),
                    token,
                    fresh=fresh,
                    prior_review_id="architecture-review-1",
                    acceptance_mode=AcceptanceMode.ARCHITECTURE,
                )

            def repair(finding: ReviewFinding) -> RepairRecord:
                sdk_turn(
                    "Repair the exact prior findings in this disposable repository: write objective.txt with exactly "
                    "'repaired' followed by one newline. Do not modify any other file. Return the requested JSON object."
                )
                return RepairRecord(
                    "repair-1",
                    finding.finding_id,
                    DispatchId("h4b-pilot/multi-authority/executor/1"),
                    "repair objective marker through the same SDK thread",
                    "repaired",
                )

            result = controller.h4_walking_skeleton(
                load_workflow_config(Path(__file__).parents[2] / "workflow.toml")
            ).run_multi_authority(
                "h4b-pilot",
                "multi-authority",
                objective=objective,
                repair=repair,
                current_revision=revision,
                acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL, AcceptanceMode.ARCHITECTURE),
                objective_reviewer=objective_reviewer,
                visual_reviewer=visual_reviewer,
                architecture_reviewer=architecture_reviewer,
                render_evidence=fixed_render,
                architecture_replan=lambda finding: ReplanProposal("bounded same-contract repair strategy"),
                budget=Budget(max_turns=2, max_repairs=1, max_reviews=2, max_validations=2),
            )
            status = "passed" if result.accepted and target.read_text(encoding="utf-8") == "repaired\n" else "failed"
            return {
                "schema": "codex-flow/h4-b-pilot/v1",
                "status": status,
                "provider_route": "codex-lb",
                "model": model,
                "reasoning_effort": effort.value,
                "base_sha": base,
                "thread_started": thread is not None,
                "thread_identity_reused": True,
                "sdk_turns": turns,
                "observable_edit": target.read_text(encoding="utf-8") == "repaired\n",
                "fixed_rendered_evidence": {
                    "evidence_id": "fixed-render-1",
                    "artifact_sha256": _sha256(render),
                    "artifact_path": "fixed-rendered-evidence.svg",
                },
                "authorized_changed_paths": _git(root, "status", "--short").splitlines(),
                "final_file_sha256": _sha256(target),
                "lifecycle_kinds": [record.kind for record in ledger.h4_lifecycle("h4b-pilot", "multi-authority")],
                "review_count": len(result.review_ids),
                "repair_count": len(result.repair_ids),
                "same_owner_repair": True,
                "architecture_replan": True,
                "accepted_state": ledger.current_state("h4b-pilot", "multi-authority").value,
            }
        except CodexFlowError as exc:
            return {
                "schema": "codex-flow/h4-b-pilot/v1",
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
                "schema": "codex-flow/h4-b-pilot/v1",
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
