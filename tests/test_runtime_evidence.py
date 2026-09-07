from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import codex_flow.controller as controller_module
import codex_flow.harness as harness_module
import codex_flow.runtime_evidence as runtime_evidence_module
from codex_flow.contracts import (
    ModelFacingProgramControllerAction,
    ModelFacingProgramControllerActionBundle,
    model_facing_result_schema,
)
from codex_flow.controller import capsule_json
from codex_flow.domain import (
    AcceptanceMode,
    DispatchId,
    ExecutionCapsule,
    Generation,
    MilestoneId,
    NativePermissionMode,
    ProgramApprovalGate,
    ProgramControllerActionKind,
    ProgramControllerDecisionStatus,
    ProgramEventKind,
    ProgramGraph,
    ProgramId,
    ProgramIntegrationMode,
    ProgramNodeSpec,
    ProgramOutcomeKind,
    ReasoningEffort,
    ReviewResult,
    RoleId,
    RunId,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
)
from codex_flow.harness import WorkflowHarness
from codex_flow.ledger import Ledger, StaleWriter
from codex_flow.runtime_evidence import (
    RuntimeEvidenceError,
    RuntimeEvidenceManifest,
    capture_runtime_evidence,
    verify_runtime_evidence,
)
from codex_flow.worktrees import WorktreeError


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repository(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "runtime-evidence@example.test")
    _git(root, "config", "user.name", "Runtime Evidence Test")
    (root / "protected.txt").write_text("protected\n")
    (root / "evidence" / "nested").mkdir(parents=True)
    (root / "evidence" / "nested" / "report.json").write_text('{"ok":true}\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def _runtime_capsule(root: Path, source_head: str) -> ExecutionCapsule:
    return ExecutionCapsule(
        2,
        RunId("program"),
        MilestoneId("runtime"),
        root,
        WorkspaceMode.CURRENT_CHECKOUT,
        root,
        "main",
        source_head,
        "program",
        (),
        ("protected.txt",),
        ValidationSpec(("true",), 5),
        "gpt-test",
        ReasoningEffort.MEDIUM,
        "run the bounded runtime check and retain its report",
        model_facing_result_schema(2),
        permission_mode=NativePermissionMode.READ_ONLY,
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        outcome_kind=ProgramOutcomeKind.RUNTIME_EVIDENCE,
        integration_mode=ProgramIntegrationMode.NO_INTEGRATION,
        runtime_artifact_paths=("evidence",),
        runtime_artifact_max_files=4,
        runtime_artifact_max_bytes=4_096,
        acceptance_criteria_sha256="a" * 64,
    )


def _manifest(
    capsule: ExecutionCapsule, source_head: str, *, terminal_outcome: str = "completed"
) -> RuntimeEvidenceManifest:
    capsule_digest = hashlib.sha256(
        json.dumps(capsule_json(capsule), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RuntimeEvidenceManifest(
        1,
        "program",
        "runtime",
        "program/runtime/executor/1",
        1,
        1,
        "b" * 64,
        capsule_digest,
        source_head,
        "d" * 64,
        ("true",),
        terminal_outcome,
        "a" * 64,
        (),
    )


def _runtime_graph(root: Path) -> tuple[ProgramGraph, ExecutionCapsule, str]:
    source_head = _repository(root)
    capsule = _runtime_capsule(root, source_head)
    graph = ProgramGraph(
        ProgramId("program"),
        root / "canonical-plan.md",
        "b" * 64,
        (ProgramNodeSpec(MilestoneId("runtime"), capsule),),
        source_head,
    )
    return graph, capsule, source_head


def _gate_test_graph(root: Path) -> ProgramGraph:
    source_head = _repository(root)
    runtime = _runtime_capsule(root, source_head)
    downstream = ExecutionCapsule(
        runtime.capsule_version,
        runtime.run_id,
        MilestoneId("downstream"),
        runtime.repository_root,
        runtime.workspace_mode,
        runtime.workspace_path,
        runtime.branch,
        runtime.base_sha,
        runtime.lane,
        ("downstream",),
        runtime.protected_paths,
        runtime.validation,
        runtime.model,
        runtime.reasoning_effort,
        runtime.prompt,
        runtime.output_schema,
        runtime.permission_mode,
        runtime.acceptance_modes,
        runtime.plugin_requirements,
        runtime.local_image_paths,
        runtime.outcome_kind,
        runtime.integration_mode,
        runtime.runtime_artifact_paths,
        runtime.runtime_artifact_max_files,
        runtime.runtime_artifact_max_bytes,
        (),
        runtime.acceptance_criteria_sha256,
    )
    return ProgramGraph(
        ProgramId("program"),
        root / "canonical-plan.md",
        "b" * 64,
        (
            ProgramNodeSpec(MilestoneId("runtime"), runtime),
            ProgramNodeSpec(MilestoneId("downstream"), downstream, (MilestoneId("runtime"),)),
        ),
        source_head,
    )


def _inject_retained_approval_gate(ledger: Ledger) -> None:
    row = ledger._db().execute("SELECT program_graph_json FROM runs WHERE run_id = 'program'").fetchone()
    assert row is not None
    graph = json.loads(str(row["program_graph_json"]))
    graph["nodes"][0]["capsule"]["approval_gates"] = [
        ProgramApprovalGate(
            "operator-approval",
            (MilestoneId("downstream"),),
            (MilestoneId("runtime"),),
            "The retained graph requires an operator decision.",
        ).to_json()
    ]
    ledger._db().execute(
        "UPDATE runs SET program_graph_json = ? WHERE run_id = 'program'",
        (json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
    )


def _submit_program_action(
    ledger: Ledger, status: ProgramControllerDecisionStatus, action: ModelFacingProgramControllerAction
) -> tuple[ModelFacingProgramControllerActionBundle, object, object]:
    decision_id = status.decision_id
    claim = ledger.claim_program_controller_decision(
        decision_id,
        claimant_id="program-controller/runtime-test",
        expected_revision=status.revision,
        generation=int(status.current_generation),
    )
    bundle = ModelFacingProgramControllerActionBundle(
        1,
        decision_id,
        status.program_id,
        "b" * 64,
        Generation(int(claim.generation)),
        status.event_kind,
        status.event_key,
        status.payload["program_revision"],
        status.payload["trunk_head"],
        (action,),
        "bounded runtime evidence lifecycle test",
    )
    receipt = ledger.submit_program_controller_actions(
        bundle,
        claimant_id=claim.claimant_id,
        token=str(claim.token),
    )
    return bundle, receipt, claim


def _apply_program_action(
    ledger: Ledger, status: ProgramControllerDecisionStatus, action: ModelFacingProgramControllerAction
) -> None:
    bundle, receipt, claim = _submit_program_action(ledger, status, action)
    if action.kind is ProgramControllerActionKind.PROMOTE_CANDIDATE and action.evidence_sha256 is not None:
        manifest = ledger.runtime_evidence_manifest("program", action.milestone_id or "", action.evidence_sha256)
        artifact_root = ledger.path.parent / "artifacts"
        if not artifact_root.is_dir():
            artifact_root = ledger.path.parent / ".codex-flow" / "artifacts"
        verify_runtime_evidence(
            runtime_evidence_module.RuntimeEvidenceSnapshot(
                manifest,
                artifact_root / "runtime-evidence" / action.evidence_sha256,
            )
        )
        ledger.close_program_node(
            "program",
            action.milestone_id or "",
            subject=action.subject_wire or "",
            review_ids=action.review_ids,
            expected_program_revision=int(status.payload["program_revision"]) + 1,
        )
    for effect_id, _milestone_id in bundle.external_effects:
        ledger.record_program_action_effect(bundle.action_id, effect_id, state="applied")
    ledger.acknowledge_controller_action(
        bundle.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
        claimant_id=claim.claimant_id,
        token=str(claim.token),
    )
    ledger.complete_controller_generation(
        bundle.decision_id,
        generation=int(claim.generation),
        state="completed",
    )


def _runtime_review_ready(
    tmp_path: Path,
    graph: ProgramGraph | None = None,
) -> tuple[
    Ledger, ExecutionCapsule, str, runtime_evidence_module.RuntimeEvidenceSnapshot, ProgramControllerDecisionStatus
]:
    if graph is None:
        graph, capsule, source_head = _runtime_graph(tmp_path)
    else:
        capsule = graph.node("runtime").capsule
        source_head = graph.trunk_head
    state_dir = tmp_path / ".codex-flow"
    state_dir.mkdir()
    ledger = Ledger(state_dir / "workflow.db")
    ledger.register_program(graph)
    start = ledger.start_program("program")
    _apply_program_action(
        ledger,
        start,
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.START_READY_MILESTONES,
            milestone_ids=("runtime",),
        ),
    )
    ledger.claim_dispatch("program", "runtime", "executor", 1)
    manifest = _manifest(capsule, source_head)
    snapshot = capture_runtime_evidence(
        tmp_path,
        ("evidence",),
        max_files=4,
        max_bytes=4_096,
        manifest=manifest,
        snapshot_root=tmp_path / ".codex-flow" / "artifacts" / "runtime-evidence",
    )
    ledger.record_runtime_evidence(
        "program",
        "runtime",
        manifest=snapshot.manifest,
        terminal_status="completed",
        dispatch_id=DispatchId("program/runtime/executor/1"),
    )
    implementation = next(
        item
        for item in ledger.program_controller_decisions()
        if item.event_kind is ProgramEventKind.IMPLEMENTATION_COMPLETED
    )
    _apply_program_action(
        ledger,
        implementation,
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.START_REVIEWS,
            milestone_id="runtime",
            evidence_sha256=snapshot.digest,
            review_roles=("architecture-reviewer", "code-reviewer"),
        ),
    )
    subject = f"runtime_evidence:{snapshot.digest}"
    ledger.record_program_review(
        "program",
        "runtime",
        ReviewResult(
            "runtime-code", RoleId("code-reviewer"), True, (), subject, acceptance_mode=AcceptanceMode.OBJECTIVE
        ),
    )
    ledger.record_program_review(
        "program",
        "runtime",
        ReviewResult(
            "runtime-architecture",
            RoleId("architecture-reviewer"),
            True,
            (),
            subject,
            acceptance_mode=AcceptanceMode.ARCHITECTURE,
        ),
    )
    review_completion = next(
        item for item in ledger.program_controller_decisions() if item.event_kind is ProgramEventKind.REVIEW_COMPLETED
    )
    return ledger, capsule, source_head, snapshot, review_completion


def _transitive_runtime_graph(root: Path) -> ProgramGraph:
    graph, runtime, source_head = _runtime_graph(root)
    downstream = replace(
        runtime,
        milestone_id=MilestoneId("downstream"),
        mutable_paths=("downstream.txt",),
        output_schema=model_facing_result_schema(1),
        outcome_kind=ProgramOutcomeKind.COMMIT,
        integration_mode=ProgramIntegrationMode.GIT,
        runtime_artifact_paths=(),
        runtime_artifact_max_files=None,
        runtime_artifact_max_bytes=None,
    )
    return replace(
        graph,
        nodes=(
            graph.node("runtime"),
            ProgramNodeSpec(MilestoneId("downstream"), downstream, (MilestoneId("runtime"),)),
        ),
        trunk_head=source_head,
    )


def _transitive_runtime_ready(
    tmp_path: Path,
) -> tuple[Ledger, ExecutionCapsule, str, runtime_evidence_module.RuntimeEvidenceSnapshot]:
    graph = _transitive_runtime_graph(tmp_path)
    ledger, capsule, source_head, snapshot, review_completion = _runtime_review_ready(tmp_path, graph)
    _apply_program_action(
        ledger,
        review_completion,
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.PROMOTE_CANDIDATE,
            milestone_id="runtime",
            evidence_sha256=snapshot.digest,
            review_ids=("runtime-architecture", "runtime-code"),
        ),
    )
    return ledger, capsule, source_head, snapshot


def _downstream_start_request(
    ledger: Ledger,
) -> tuple[dict[str, object], ModelFacingProgramControllerActionBundle]:
    decision = ledger.start_program("program", event_key="start-downstream")
    claim = ledger.claim_program_controller_decision(
        decision.decision_id,
        claimant_id="program-controller/transitive-runtime-test",
        expected_revision=decision.revision,
        generation=int(decision.current_generation),
    )
    bundle = ModelFacingProgramControllerActionBundle(
        1,
        decision.decision_id,
        decision.program_id,
        str(decision.payload["plan_digest"]),
        Generation(int(claim.generation)),
        decision.event_kind,
        decision.event_key,
        decision.payload["program_revision"],
        decision.payload["trunk_head"],
        (
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_READY_MILESTONES,
                milestone_ids=("downstream",),
            ),
        ),
        "start the transitive runtime successor",
    )
    return {
        "version": 1,
        "operation": "program_submit_actions",
        "bundle": bundle.to_json(),
        "claimant_id": claim.claimant_id,
        "token": str(claim.token),
    }, bundle


def test_runtime_evidence_capture_is_nested_content_addressed_and_rehashable(tmp_path: Path) -> None:
    source_head = _repository(tmp_path)
    manifest = _manifest(_runtime_capsule(tmp_path, source_head), source_head)
    destination = tmp_path / ".codex-flow" / "artifacts" / "runtime-evidence"

    snapshot = capture_runtime_evidence(
        tmp_path,
        ("evidence",),
        max_files=4,
        max_bytes=4_096,
        manifest=manifest,
        snapshot_root=destination,
    )

    assert snapshot.root == destination / snapshot.digest
    assert snapshot.manifest.files[0].relative_path == "evidence/nested/report.json"
    assert verify_runtime_evidence(snapshot) is True
    (tmp_path / "evidence" / "nested" / "report.json").write_text('{"ok":false}\n')
    assert verify_runtime_evidence(snapshot) is True

    with pytest.raises(RuntimeEvidenceError, match="member bytes changed"):
        snapshot.manifest_path.parent.joinpath("evidence/nested/report.json").write_text("tampered\n")
        verify_runtime_evidence(snapshot)


def test_runtime_evidence_rejects_symlinks_hardlinks_and_budget_overflow(tmp_path: Path) -> None:
    source_head = _repository(tmp_path)
    manifest = _manifest(_runtime_capsule(tmp_path, source_head), source_head)
    (tmp_path / "evidence" / "link.json").symlink_to("nested/report.json")
    with pytest.raises(RuntimeEvidenceError, match="symlink"):
        capture_runtime_evidence(
            tmp_path,
            ("evidence",),
            max_files=4,
            max_bytes=4_096,
            manifest=manifest,
            snapshot_root=tmp_path / "snapshots",
        )

    (tmp_path / "evidence" / "link.json").unlink()
    os.link(tmp_path / "evidence" / "nested" / "report.json", tmp_path / "evidence" / "alias.json")
    with pytest.raises(RuntimeEvidenceError, match="single-link"):
        capture_runtime_evidence(
            tmp_path,
            ("evidence",),
            max_files=4,
            max_bytes=4_096,
            manifest=manifest,
            snapshot_root=tmp_path / "snapshots",
        )

    with pytest.raises(RuntimeEvidenceError, match="max_files"):
        capture_runtime_evidence(
            tmp_path,
            ("evidence/nested/report.json",),
            max_files=0,
            max_bytes=4_096,
            manifest=manifest,
            snapshot_root=tmp_path / "snapshots",
        )


def test_runtime_evidence_publication_retains_failed_staging_for_forensics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_head = _repository(tmp_path)
    manifest = _manifest(_runtime_capsule(tmp_path, source_head), source_head)
    destination = tmp_path / "snapshots"
    original_write = runtime_evidence_module.os.write
    calls = 0

    def fail_first_write(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected publication failure")
        return original_write(descriptor, data)

    monkeypatch.setattr(runtime_evidence_module.os, "write", fail_first_write)
    with pytest.raises(RuntimeEvidenceError, match="publication failed"):
        capture_runtime_evidence(
            tmp_path,
            ("evidence",),
            max_files=4,
            max_bytes=4_096,
            manifest=manifest,
            snapshot_root=destination,
        )
    assert any(item.name.startswith(".runtime-evidence-") for item in destination.iterdir())


def test_runtime_program_closure_is_evidence_bound_and_never_creates_git_integration(
    tmp_path: Path,
) -> None:
    graph, capsule, source_head = _runtime_graph(tmp_path)
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(graph)
        start = ledger.start_program("program")
        _apply_program_action(
            ledger,
            start,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_READY_MILESTONES,
                milestone_ids=("runtime",),
            ),
        )
        ledger.claim_dispatch("program", "runtime", "executor", 1)
        manifest = _manifest(capsule, source_head)
        snapshot = capture_runtime_evidence(
            tmp_path,
            ("evidence",),
            max_files=4,
            max_bytes=4_096,
            manifest=manifest,
            snapshot_root=tmp_path / ".codex-flow" / "artifacts" / "runtime-evidence",
        )
        recorded = ledger.record_runtime_evidence(
            "program",
            "runtime",
            manifest=snapshot.manifest,
            terminal_status="completed",
            dispatch_id=DispatchId("program/runtime/executor/1"),
        )
        assert recorded.nodes[0].evidence_sha256 == snapshot.digest
        assert recorded.nodes[0].closure_satisfied is False

        implementation = next(
            item
            for item in ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.IMPLEMENTATION_COMPLETED
        )
        _apply_program_action(
            ledger,
            implementation,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_REVIEWS,
                milestone_id="runtime",
                evidence_sha256=snapshot.digest,
                review_roles=("architecture-reviewer", "code-reviewer"),
            ),
        )
        subject = f"runtime_evidence:{snapshot.digest}"
        ledger.record_program_review(
            "program",
            "runtime",
            ReviewResult(
                "runtime-code", RoleId("code-reviewer"), True, (), subject, acceptance_mode=AcceptanceMode.OBJECTIVE
            ),
        )
        ledger.record_program_review(
            "program",
            "runtime",
            ReviewResult(
                "runtime-architecture",
                RoleId("architecture-reviewer"),
                True,
                (),
                subject,
                acceptance_mode=AcceptanceMode.ARCHITECTURE,
            ),
        )
        review_completion = next(
            item
            for item in ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.REVIEW_COMPLETED
        )
        _apply_program_action(
            ledger,
            review_completion,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.PROMOTE_CANDIDATE,
                milestone_id="runtime",
                evidence_sha256=snapshot.digest,
                review_ids=("runtime-architecture", "runtime-code"),
            ),
        )
        status = ledger.program_status("program")
        assert status.state.value == "completed"
        assert status.nodes[0].closure_satisfied is True
        assert status.nodes[0].integrated is False
        assert status.nodes[0].candidate_sha is None
        assert ledger.pending_program_integrations("program") == ()
        assert (
            ledger._db().execute("SELECT COUNT(*) FROM integration_outbox WHERE program_id = 'program'").fetchone()[0]
            == 0
        )
    finally:
        ledger.close()


def test_runtime_evidence_failed_terminal_is_retained_with_a_blocker_and_exact_replay(tmp_path: Path) -> None:
    graph, capsule, source_head = _runtime_graph(tmp_path)
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(graph)
        ledger.claim_dispatch("program", "runtime", "executor", 1)
        manifest = _manifest(capsule, source_head, terminal_outcome="failed")
        first = ledger.record_runtime_evidence(
            "program",
            "runtime",
            manifest=manifest,
            terminal_status="failed",
            dispatch_id="program/runtime/executor/1",
            result_sha256="e" * 64,
        )
        replay = ledger.record_runtime_evidence(
            "program",
            "runtime",
            manifest=manifest,
            terminal_status="failed",
            dispatch_id="program/runtime/executor/1",
            result_sha256="e" * 64,
        )
        assert replay.revision == first.revision
        assert replay.nodes[0].state == WorkflowState.FAILED.value
        assert replay.nodes[0].blocker is not None
        assert replay.nodes[0].evidence_sha256 == manifest.sha256
        with pytest.raises(StaleWriter, match="terminal replay conflicts"):
            ledger.record_runtime_evidence(
                "program",
                "runtime",
                manifest=RuntimeEvidenceManifest(
                    1,
                    "program",
                    "runtime",
                    "program/runtime/executor/1",
                    1,
                    1,
                    "b" * 64,
                    manifest.capsule_sha256,
                    source_head,
                    "d" * 64,
                    ("true",),
                    "completed",
                    "a" * 64,
                    (),
                ),
                terminal_status="completed",
                dispatch_id="program/runtime/executor/1",
            )
    finally:
        ledger.close()


def test_retained_gate_graph_is_rejected_before_start_mutation(tmp_path: Path) -> None:
    graph = _gate_test_graph(tmp_path)
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(graph)
        before = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        assert before is not None
        assert ledger.program_controller_decisions() == ()

        _inject_retained_approval_gate(ledger)
        with pytest.raises(StaleWriter, match="approval_capability_unavailable"):
            ledger.start_program("program")

        after = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        assert after is not None
        assert tuple(after) == tuple(before)
        assert ledger.program_controller_decisions() == ()
    finally:
        ledger.close()


def test_retained_gate_graph_blocks_launch_and_recovered_outbox_replay(tmp_path: Path) -> None:
    graph = _gate_test_graph(tmp_path)
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(graph)
        started = ledger.start_program("program")
        claim = ledger.claim_program_controller_decision(
            started.decision_id,
            claimant_id="program-controller/runtime-test",
            expected_revision=started.revision,
            generation=int(started.current_generation),
        )
        before_launch = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        generation_before = ledger.controller_generation(started.decision_id, int(started.current_generation))
        _inject_retained_approval_gate(ledger)

        with pytest.raises(StaleWriter, match="approval_capability_unavailable"):
            ledger.prepare_controller_generation(
                started.decision_id,
                generation=started.current_generation,
            )
        generation_after = ledger.controller_generation(started.decision_id, int(started.current_generation))
        after_launch = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        assert before_launch is not None and after_launch is not None
        assert tuple(after_launch) == tuple(before_launch)
        assert generation_after == generation_before

        # A committed action is retained before the graph is imported/mutated;
        # replay must hit the same closed capability guard before returning it.
        # Use a fresh gate-free program decision revision for this outbox proof.
        ledger._db().execute(
            "UPDATE runs SET program_graph_json = ? WHERE run_id = 'program'",
            (
                json.dumps(
                    ledger._program_graph_projection(graph), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            ),
        )
        action = ModelFacingProgramControllerAction(ProgramControllerActionKind.ACKNOWLEDGE_ONLY)
        bundle = ModelFacingProgramControllerActionBundle(
            1,
            started.decision_id,
            started.program_id,
            "b" * 64,
            Generation(int(claim.generation)),
            started.event_kind,
            started.event_key,
            started.payload["program_revision"],
            started.payload["trunk_head"],
            (action,),
            "retained outbox replay capability proof",
        )
        inspection_claim = ledger.reserve_controller_recovery_inspection(started.decision_id)
        ledger.complete_controller_recovery_inspection(
            started.decision_id,
            inspection_outcome="completed",
            claim=inspection_claim,
            bundle=bundle,
        )
        receipt = ledger.submit_recovered_program_controller_actions(started.decision_id)
        _inject_retained_approval_gate(ledger)
        before_replay = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        with pytest.raises(StaleWriter, match="approval_capability_unavailable"):
            ledger.submit_recovered_program_controller_actions(started.decision_id)
        after_replay = (
            ledger._db().execute("SELECT program_state, program_revision FROM runs WHERE run_id = 'program'").fetchone()
        )
        assert before_replay is not None and after_replay is not None
        assert tuple(after_replay) == tuple(before_replay)
        assert receipt.action_id == bundle.action_id
        harness = WorkflowHarness(tmp_path)
        try:
            assert harness._reconcile_program_action_outboxes() is False
        finally:
            harness.close()
    finally:
        ledger.close()


@pytest.mark.parametrize("damage", ("missing", "corrupt"))
def test_runtime_promotion_rehash_blocks_missing_or_corrupt_snapshot(tmp_path: Path, damage: str) -> None:
    ledger, _capsule, _source_head, snapshot, review_completion = _runtime_review_ready(tmp_path)
    harness = WorkflowHarness(tmp_path)
    try:
        bundle, _receipt, _claim = _submit_program_action(
            ledger,
            review_completion,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.PROMOTE_CANDIDATE,
                milestone_id="runtime",
                evidence_sha256=snapshot.digest,
                review_ids=("runtime-architecture", "runtime-code"),
            ),
        )
        if damage == "missing":
            shutil.rmtree(snapshot.root)
        else:
            snapshot.manifest_path.write_text("corrupt\n")

        with pytest.raises(WorktreeError, match="pre-acceptance rehash"):
            harness._apply_program_action_bundle(bundle)
        status = ledger.program_status("program")
        assert status.nodes[0].closure_satisfied is False
        assert status.nodes[0].blocker is not None
        assert ledger.program_action_effect_state(bundle.action_id, bundle.external_effects[0][0]) == "pending"
    finally:
        harness.close()
        ledger.close()


def test_recovered_runtime_promotion_rehashes_before_closure(tmp_path: Path) -> None:
    ledger, _capsule, _source_head, snapshot, review_completion = _runtime_review_ready(tmp_path)
    harness = WorkflowHarness(tmp_path)
    try:
        claim = ledger.claim_program_controller_decision(
            review_completion.decision_id,
            claimant_id="program-controller/runtime-test",
            expected_revision=review_completion.revision,
            generation=int(review_completion.current_generation),
        )
        bundle = ModelFacingProgramControllerActionBundle(
            1,
            review_completion.decision_id,
            review_completion.program_id,
            "b" * 64,
            Generation(int(claim.generation)),
            review_completion.event_kind,
            review_completion.event_key,
            review_completion.payload["program_revision"],
            review_completion.payload["trunk_head"],
            (
                ModelFacingProgramControllerAction(
                    ProgramControllerActionKind.PROMOTE_CANDIDATE,
                    milestone_id="runtime",
                    evidence_sha256=snapshot.digest,
                    review_ids=("runtime-architecture", "runtime-code"),
                ),
            ),
            "bounded runtime evidence recovery promotion proof",
        )
        inspection_claim = ledger.reserve_controller_recovery_inspection(review_completion.decision_id)
        ledger.complete_controller_recovery_inspection(
            review_completion.decision_id,
            inspection_outcome="completed",
            claim=inspection_claim,
            bundle=bundle,
        )
        recovered = ledger.submit_recovered_program_controller_actions(review_completion.decision_id)
        assert recovered.action_id == bundle.action_id
        assert harness._apply_program_action_outbox(bundle.action_id) is True
        status = ledger.program_status("program")
        assert status.state.value == "completed"
        assert status.nodes[0].closure_satisfied is True
    finally:
        harness.close()
        ledger.close()


def test_public_recovered_program_submission_restarts_before_commit_and_replays_after_commit(
    tmp_path: Path,
) -> None:
    ledger, _capsule, _source_head, snapshot, review_completion = _runtime_review_ready(tmp_path)
    claim = ledger.claim_program_controller_decision(
        review_completion.decision_id,
        claimant_id="program-controller/runtime-recovery-test",
        expected_revision=review_completion.revision,
        generation=int(review_completion.current_generation),
    )
    bundle = ModelFacingProgramControllerActionBundle(
        1,
        review_completion.decision_id,
        review_completion.program_id,
        "b" * 64,
        Generation(int(claim.generation)),
        review_completion.event_kind,
        review_completion.event_key,
        review_completion.payload["program_revision"],
        review_completion.payload["trunk_head"],
        (
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.PROMOTE_CANDIDATE,
                milestone_id="runtime",
                evidence_sha256=snapshot.digest,
                review_ids=("runtime-architecture", "runtime-code"),
            ),
        ),
        "recover the exact runtime promotion",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(review_completion.decision_id)
    ledger.complete_controller_recovery_inspection(
        review_completion.decision_id,
        inspection_outcome="completed",
        claim=inspection_claim,
        bundle=bundle,
    )
    assert not any(
        item["decision_id"] == str(review_completion.decision_id) for item in ledger.program_action_outboxes("program")
    )
    ledger.close()

    request = {
        "version": 1,
        "operation": "program_submit_recovered_actions",
        "decision_id": str(review_completion.decision_id),
    }
    harness = WorkflowHarness(tmp_path)
    try:
        first = harness._controller_request(request)
        assert first["ok"] is True
        first_outboxes = tuple(
            item
            for item in harness.ledger.program_action_outboxes("program")
            if item["decision_id"] == str(review_completion.decision_id)
        )
        assert len(first_outboxes) == 1
        assert first_outboxes[0]["action_id"] == bundle.action_id
        assert harness.ledger.program_action_effect_state(bundle.action_id, bundle.external_effects[0][0]) == "applied"
        assert harness.ledger.program_status("program").nodes[0].closure_satisfied is True
    finally:
        harness.close()

    restarted = WorkflowHarness(tmp_path)
    try:
        replay = restarted._controller_request(request)
        assert replay["ok"] is True
        assert replay["receipt"]["action_id"] == first["receipt"]["action_id"]
        assert (
            len(
                tuple(
                    item
                    for item in restarted.ledger.program_action_outboxes("program")
                    if item["decision_id"] == str(review_completion.decision_id)
                )
            )
            == 1
        )
        assert (
            sum(fact.kind == "promotion_accepted" for fact in restarted.ledger.review_lifecycle("program", "runtime"))
            == 1
        )
    finally:
        restarted.close()


def test_retained_approval_gate_blocks_program_queue_claim_without_mutation(tmp_path: Path) -> None:
    ledger, _capsule, _source_head, _snapshot = _transitive_runtime_ready(tmp_path)
    ledger.close()
    harness = WorkflowHarness(tmp_path)
    try:
        request, _bundle = _downstream_start_request(harness.ledger)
        assert harness._controller_request(request)["ok"] is True
        authority = harness.acquire()
        before = harness.ledger.queue_dispatch("program/downstream/executor/1")
        _inject_retained_approval_gate(harness.ledger)

        with pytest.raises(StaleWriter, match="approval_capability_unavailable"):
            harness.ledger.claim_queue_dispatch(
                epoch=int(authority["epoch"]),
                claim_nonce_sha256="e" * 64,
            )

        assert harness.ledger.queue_dispatch("program/downstream/executor/1") == before
    finally:
        harness.close()


def test_retained_approval_gate_blocks_recovery_continuation_without_mutation(tmp_path: Path) -> None:
    ledger, _capsule, _source_head, _snapshot = _transitive_runtime_ready(tmp_path)
    ledger.close()
    harness = WorkflowHarness(tmp_path)
    try:
        request, _bundle = _downstream_start_request(harness.ledger)
        assert harness._controller_request(request)["ok"] is True
        authority = harness.acquire()
        dispatch_id = "program/downstream/executor/1"
        epoch = int(authority["epoch"])
        assert harness.ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="f" * 64) is not None
        harness.ledger.set_queue_state(dispatch_id, "starting", epoch=epoch)
        queue = harness.ledger.queue_dispatch(dispatch_id)
        token = "approval-recovery-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        harness.ledger.issue_attempt_capability(
            dispatch_id,
            generation=int(queue["generation"]),
            attempt=int(queue["attempt"]),
            operation="submit_result",
            schema_sha256=str(queue["result_contract_sha256"]),
            workspace_path=tmp_path,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        harness.ledger.bind_worker_liveness(
            dispatch_id,
            epoch=epoch,
            generation=int(queue["generation"]),
            attempt=int(queue["attempt"]),
            pid=os.getpid(),
            process_birth_identity="approval-recovery-worker",
            lease_token_sha256=token_sha256,
        )
        harness.ledger.mark_worker_exit(
            dispatch_id,
            pid=os.getpid(),
            process_birth_identity="approval-recovery-worker",
            exit_code=79,
            classification="transient-after-identity",
        )
        harness.ledger.record_recovery_inspection(
            dispatch_id,
            kind="transient_failed_turn",
            thread_id="approval-recovery-thread",
            turn_id="approval-recovery-turn",
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        assert harness.ledger.queue_dispatch(dispatch_id)["state"] == "recovery_continuation_pending"
        before_queue = harness.ledger.queue_dispatch(dispatch_id)
        before_recovery = harness.ledger.recovery_state(dispatch_id)
        before_policy = harness.ledger.retry_policy(dispatch_id)
        _inject_retained_approval_gate(harness.ledger)

        with pytest.raises(StaleWriter, match="approval_capability_unavailable"):
            harness.ledger.begin_recovery_continuation(
                dispatch_id,
                epoch=epoch,
                claim_nonce_sha256="a" * 64,
                now="2000-01-01T00:00:01Z",
            )

        assert harness.ledger.queue_dispatch(dispatch_id) == before_queue
        assert harness.ledger.recovery_state(dispatch_id) == before_recovery
        assert harness.ledger.retry_policy(dispatch_id) == before_policy
    finally:
        harness.close()


def test_transitive_runtime_predecessor_corruption_blocks_public_enqueue(tmp_path: Path) -> None:
    ledger, _capsule, _source_head, snapshot = _transitive_runtime_ready(tmp_path)
    ledger.close()
    harness = WorkflowHarness(tmp_path)
    try:
        request, _bundle = _downstream_start_request(harness.ledger)
        snapshot.manifest_path.write_text("corrupt\n")

        with pytest.raises(WorktreeError, match="pre-acceptance rehash"):
            harness._controller_request(request)

        assert (
            harness.ledger._db()
            .execute("SELECT COUNT(*) FROM dispatch_queue WHERE run_id = 'program' AND milestone_id = 'downstream'")
            .fetchone()[0]
            == 0
        )
        status = harness.ledger.program_status("program")
        runtime_status = next(node for node in status.nodes if node.milestone_id == "runtime")
        assert runtime_status.closure_satisfied is False
        assert runtime_status.blocker is not None
        assert any(
            fact.kind == "evidence_invalidated" for fact in harness.ledger.review_lifecycle("program", "runtime")
        )
        assert any(
            decision.event_kind is ProgramEventKind.CONTROLLER_ATTENTION and decision.event_key.endswith("/invalidated")
            for decision in harness.ledger.program_controller_decisions()
        )
        assert not any(
            any(
                action.kind is ProgramControllerActionKind.START_READY_MILESTONES
                and "downstream" in action.milestone_ids
                for action in ModelFacingProgramControllerActionBundle.from_json_bytes(str(item["bundle_json"])).actions
            )
            for item in harness.ledger.program_action_outboxes("program")
            if item.get("bundle_json") is not None
        )
    finally:
        harness.close()


def _patch_trusted_program_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    real_controller = controller_module.Controller

    class TestController(real_controller):
        def __init__(self, state_root: Path, *, worktrees: object = None) -> None:
            super().__init__(
                state_root,
                _trusted_test_adapter_factory=lambda _config: object(),
                worktrees=worktrees,  # type: ignore[arg-type]
            )

    monkeypatch.setattr(controller_module, "Controller", TestController)


def test_transitive_runtime_predecessor_corruption_blocks_queued_launch_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _capsule, _source_head, snapshot = _transitive_runtime_ready(tmp_path)
    ledger.close()
    _patch_trusted_program_controller(monkeypatch)
    harness = WorkflowHarness(tmp_path)
    popen_calls = 0

    real_popen = harness_module.subprocess.Popen

    def unexpected_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_calls
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, tuple | list) and command and command[0] == "git":
            return real_popen(*args, **kwargs)
        popen_calls += 1
        raise AssertionError("corrupt transitive evidence must block Popen")

    monkeypatch.setattr(harness_module.subprocess, "Popen", unexpected_popen)
    try:
        request, _bundle = _downstream_start_request(harness.ledger)
        response = harness._controller_request(request)
        assert response["ok"] is True
        queued = harness.ledger.queue_dispatch("program/downstream/executor/1")
        assert queued["state"] == "queued"

        authority = harness.acquire()
        claimed = harness.ledger.claim_queue_dispatch(
            epoch=int(authority["epoch"]),
            claim_nonce_sha256="c" * 64,
        )
        assert claimed is not None
        snapshot.manifest_path.write_text("corrupt\n")

        with pytest.raises(WorktreeError, match="pre-acceptance rehash"):
            harness._spawn_one(claimed)

        assert popen_calls == 0
        assert harness.ledger.queue_dispatch("program/downstream/executor/1")["state"] == "human_attention_required"
        runtime_status = next(
            node for node in harness.ledger.program_status("program").nodes if node.milestone_id == "runtime"
        )
        assert runtime_status.closure_satisfied is False
        assert runtime_status.blocker is not None
    finally:
        harness.close()


def test_unchanged_transitive_runtime_evidence_allows_one_worker_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _capsule, _source_head, _snapshot = _transitive_runtime_ready(tmp_path)
    ledger.close()
    _patch_trusted_program_controller(monkeypatch)
    harness = WorkflowHarness(tmp_path)
    try:
        request, _bundle = _downstream_start_request(harness.ledger)
        assert harness._controller_request(request)["ok"] is True
        authority = harness.acquire()
        claimed = harness.ledger.claim_queue_dispatch(
            epoch=int(authority["epoch"]),
            claim_nonce_sha256="d" * 64,
        )
        assert claimed is not None
        popen_calls = 0

        class LiveChild:
            pid = os.getpid()

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                return None

            def wait(self, *, timeout: float) -> int:
                assert timeout > 0
                return 0

        def one_popen(*_args: object, **_kwargs: object) -> LiveChild:
            nonlocal popen_calls
            popen_calls += 1
            return LiveChild()

        monkeypatch.setattr(harness_module.subprocess, "Popen", one_popen)
        harness._spawn_one(claimed)
        assert popen_calls == 1
        assert harness.ledger.queue_dispatch("program/downstream/executor/1")["state"] == "starting"
    finally:
        harness.close()
