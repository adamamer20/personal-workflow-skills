from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
        verify_runtime_evidence(
            runtime_evidence_module.RuntimeEvidenceSnapshot(
                manifest,
                ledger.path.parent / ".codex-flow" / "artifacts" / "runtime-evidence" / action.evidence_sha256,
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
) -> tuple[
    Ledger, ExecutionCapsule, str, runtime_evidence_module.RuntimeEvidenceSnapshot, ProgramControllerDecisionStatus
]:
    graph, capsule, source_head = _runtime_graph(tmp_path)
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
