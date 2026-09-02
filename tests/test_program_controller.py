from __future__ import annotations

from pathlib import Path

import pytest

from codex_flow.cli import app
from codex_flow.contracts import ModelFacingProgramControllerAction, ModelFacingProgramControllerActionBundle
from codex_flow.domain import (
    AcceptanceMode,
    ControllerActionReceipt,
    ControllerClaimantKind,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerGenerationState,
    ControllerGenerationStatus,
    ExecutionCapsule,
    Generation,
    MilestoneId,
    NativePermissionMode,
    ProgramControllerActionKind,
    ProgramControllerDecisionStatus,
    ProgramEventKind,
    ProgramGraph,
    ProgramId,
    ProgramNodeSpec,
    ReasoningEffort,
    ReviewResult,
    RoleId,
    RunId,
    ThreadIdentity,
    TurnObservation,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
)
from codex_flow.ledger import Ledger
from codex_flow.program_controller import ProgramControllerGenerationRecovery, ProgramControllerGenerationRunner

TRUNK_HEAD = "b" * 40
PLAN_DIGEST = "c" * 64
CANDIDATE_SHA = "d" * 40
NEXT_TRUNK_HEAD = "e" * 40


def _capsule(root: Path, milestone_id: str, mutable_path: str) -> ExecutionCapsule:
    return ExecutionCapsule(
        2,
        RunId("program"),
        MilestoneId(milestone_id),
        root,
        WorkspaceMode.CURRENT_CHECKOUT,
        root,
        "main",
        "a" * 40,
        "program",
        (mutable_path,),
        ("protected.txt",),
        ValidationSpec(("git", "diff", "--check"), 5),
        "gpt-test",
        ReasoningEffort.MEDIUM,
        "complete the bounded program node and return its typed result",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        permission_mode=NativePermissionMode.READ_ONLY,
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )


def _graph(root: Path) -> ProgramGraph:
    first = _capsule(root, "first", "src/first.py")
    second = _capsule(root, "second", "src/second.py")
    return ProgramGraph(
        ProgramId("program"),
        root / "canonical-plan.md",
        PLAN_DIGEST,
        (
            ProgramNodeSpec(MilestoneId("first"), first),
            ProgramNodeSpec(MilestoneId("second"), second, (MilestoneId("first"),)),
        ),
        TRUNK_HEAD,
    )


def _bundle(
    status: ProgramControllerDecisionStatus,
    claim: ControllerDecisionClaim,
    action: ModelFacingProgramControllerAction,
) -> ModelFacingProgramControllerActionBundle:
    return ModelFacingProgramControllerActionBundle(
        1,
        status.decision_id,
        status.program_id,
        PLAN_DIGEST,
        claim.generation,
        status.event_kind,
        status.event_key,
        status.payload["program_revision"],  # type: ignore[arg-type]
        TRUNK_HEAD,
        (action,),
        "bounded deterministic program effect",
    )


def _claim_and_apply(
    ledger: Ledger,
    status: ProgramControllerDecisionStatus,
    action: ModelFacingProgramControllerAction,
) -> ControllerActionReceipt:
    claim = ledger.claim_program_controller_decision(
        status.decision_id,
        claimant_id="program-controller/test",
        expected_revision=status.revision,
        generation=int(status.current_generation),
    )
    bundle = _bundle(status, claim, action)
    receipt = ledger.submit_program_controller_actions(
        bundle,
        claimant_id=claim.claimant_id,
        token=str(claim.token),
    )
    ledger.acknowledge_controller_action(
        status.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
        claimant_id=claim.claimant_id,
        token=str(claim.token),
    )
    ledger.complete_controller_generation(
        status.decision_id,
        generation=int(claim.generation),
        state=ControllerGenerationState.COMPLETED,
    )
    return receipt


def test_program_graph_rejects_cycles_and_overlapping_mutable_surfaces(tmp_path: Path) -> None:
    first = _capsule(tmp_path, "first", "src/shared.py")
    second = _capsule(tmp_path, "second", "src/shared.py")
    with pytest.raises(ValueError, match="owned by both"):
        ProgramGraph(
            ProgramId("program"),
            tmp_path / "plan.md",
            PLAN_DIGEST,
            (ProgramNodeSpec("first", first), ProgramNodeSpec("second", second)),
            TRUNK_HEAD,
        )

    with pytest.raises(ValueError, match="dependency cycle"):
        ProgramGraph(
            ProgramId("program"),
            tmp_path / "plan.md",
            PLAN_DIGEST,
            (
                ProgramNodeSpec("first", first, (MilestoneId("second"),)),
                ProgramNodeSpec("second", second, (MilestoneId("first"),)),
            ),
            TRUNK_HEAD,
        )


def test_program_lifecycle_advances_only_after_exact_review_and_integration(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        registered = ledger.register_program(_graph(tmp_path))
        assert registered.state.value == "registered"
        assert registered.ready_milestones == (MilestoneId("first"),)

        start = ledger.start_program("program")
        _claim_and_apply(
            ledger,
            start,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_READY_MILESTONES,
                milestone_ids=("first",),
            ),
        )
        ledger.claim_dispatch("program", "first", "executor", 1)
        ledger.record_program_executor_result(
            "program",
            "first",
            candidate_sha=CANDIDATE_SHA,
            terminal_status="completed",
            dispatch_id="program/first/executor/1",
        )
        implementation = next(
            item
            for item in ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.IMPLEMENTATION_COMPLETED
        )
        _claim_and_apply(
            ledger,
            implementation,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_REVIEWS,
                milestone_id="first",
                candidate_sha=CANDIDATE_SHA,
                review_roles=("architecture-reviewer", "code-reviewer"),
            ),
        )
        for review_id, role in (
            ("architecture-accepted", "architecture-reviewer"),
            ("code-accepted", "code-reviewer"),
        ):
            ledger.record_program_review(
                "program",
                "first",
                ReviewResult(
                    review_id,
                    RoleId(role),
                    True,
                    (),
                    CANDIDATE_SHA,
                    acceptance_mode=(
                        AcceptanceMode.ARCHITECTURE if role == "architecture-reviewer" else AcceptanceMode.OBJECTIVE
                    ),
                ),
            )
        review_completion = next(
            item
            for item in ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.REVIEW_COMPLETED
        )
        _claim_and_apply(
            ledger,
            review_completion,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.PROMOTE_CANDIDATE,
                milestone_id="first",
                candidate_sha=CANDIDATE_SHA,
                review_ids=("architecture-accepted", "code-accepted"),
            ),
        )
        promoted = ledger.program_status("program")
        assert promoted.nodes[0].state == WorkflowState.ACCEPTED.value
        integration_decision = ledger.record_program_event(
            "program",
            ProgramEventKind.INTEGRATION_COMPLETED,
            "integration-request",
            payload={"milestone_id": "first"},
        )
        _claim_and_apply(
            ledger,
            integration_decision,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.INTEGRATE_CANDIDATE,
                milestone_id="first",
                candidate_sha=CANDIDATE_SHA,
                expected_trunk_head=TRUNK_HEAD,
                integration_strategy="merge",
            ),
        )
        pending = ledger.pending_program_integrations("program")
        assert len(pending) == 1
        receipt = {
            "program_id": "program",
            "milestone_id": "first",
            "candidate_sha": CANDIDATE_SHA,
            "expected_trunk_head": TRUNK_HEAD,
            "strategy": "merge",
            "before_trunk_head": TRUNK_HEAD,
            "after_trunk_head": NEXT_TRUNK_HEAD,
        }
        integrated = ledger.complete_program_integration(
            "program", "first", candidate_sha=CANDIDATE_SHA, receipt=receipt
        )
        assert integrated.nodes[0].integrated is True
        assert integrated.ready_milestones == (MilestoneId("second"),)
    finally:
        ledger.close()


class _ProgramAdapter:
    def __init__(self, bundle: ModelFacingProgramControllerActionBundle) -> None:
        self.bundle = bundle
        self.thread = ThreadIdentity("program-controller-thread")
        self.bound_turns: list[str] = []

    def start_thread(self) -> ThreadIdentity:
        return self.thread

    def run_turn(
        self, _thread: ThreadIdentity, _prompt: str, *, output_schema: object, turn_callback: object
    ) -> TurnObservation:
        assert output_schema is not None
        assert callable(turn_callback)
        turn_callback("program-controller-turn")
        return TurnObservation(
            self.thread,
            "program-controller-turn",
            "completed",
            None,
            self.bundle.to_json(),
            (),
        )


class _ProgramClient:
    def __init__(
        self, status: ProgramControllerDecisionStatus, bundle: ModelFacingProgramControllerActionBundle
    ) -> None:
        self.status_value = status
        self.bundle = bundle
        self.claim_value = ControllerDecisionClaim(
            status.decision_id,
            status.current_generation,
            ControllerClaimantKind.MODEL,
            "program-controller/test",
            status.revision + 1,
            "9999-12-31T23:59:59Z",
            "program-claim-token",
        )
        self.completed: list[tuple[str, int, str]] = []

    def program_status(self, _decision_id: str) -> ProgramControllerDecisionStatus:
        return self.status_value

    def program_claim(
        self, _decision_id: str, *, claimant_id: str, expected_revision: int, generation: int | None = None
    ) -> ControllerDecisionClaim:
        assert claimant_id == self.claim_value.claimant_id
        assert expected_revision == self.status_value.revision
        assert generation == int(self.status_value.current_generation)
        return self.claim_value

    def prepare_generation(
        self, _decision_id: str, *, generation: int, prompt_sha256: str | None = None
    ) -> ControllerGenerationStatus:
        assert generation == int(self.claim_value.generation)
        return ControllerGenerationStatus(
            self.status_value.decision_id,
            Generation(generation),
            "program-lineage",
            ControllerGenerationState.DELIVERY_STARTING,
            prompt_sha256=prompt_sha256,
        )

    def bind_generation_thread(
        self, _decision_id: str, *, generation: int, controller_thread_id: str
    ) -> ControllerGenerationStatus:
        return ControllerGenerationStatus(
            self.status_value.decision_id,
            Generation(generation),
            "program-lineage",
            ControllerGenerationState.ACTIVE,
            controller_thread_id=controller_thread_id,
        )

    def bind_generation_turn(
        self,
        _decision_id: str,
        *,
        generation: int,
        controller_thread_id: str,
        controller_turn_id: str,
        previous_controller_turn_id: str | None = None,
    ) -> ControllerGenerationStatus:
        assert controller_thread_id == "program-controller-thread"
        assert previous_controller_turn_id is None
        return ControllerGenerationStatus(
            self.status_value.decision_id,
            Generation(generation),
            "program-lineage",
            ControllerGenerationState.ACTIVE,
            controller_thread_id=controller_thread_id,
            controller_turn_id=controller_turn_id,
        )

    def submit_program_actions(
        self, bundle: ModelFacingProgramControllerActionBundle, *, claimant_id: str, token: str
    ) -> ControllerActionReceipt:
        assert bundle == self.bundle
        assert claimant_id == self.claim_value.claimant_id
        assert token == self.claim_value.token
        return ControllerActionReceipt(
            bundle.action_id,
            bundle.decision_id,
            bundle.generation,
            self.claim_value.revision,
            bundle.sha256,
            {"applied_actions": ["acknowledge_only"]},
            "committed",
            "2026-09-02T00:00:00Z",
        )

    def acknowledge_program(
        self,
        _decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerActionReceipt:
        assert action_id == self.bundle.action_id
        assert bundle_sha256 == self.bundle.sha256
        assert committed_revision == self.claim_value.revision + 1
        return ControllerActionReceipt(
            action_id,
            self.bundle.decision_id,
            self.bundle.generation,
            self.claim_value.revision,
            bundle_sha256,
            {"applied_actions": ["acknowledge_only"]},
            "acknowledged",
            "2026-09-02T00:00:00Z",
            "2026-09-02T00:00:00Z",
        )

    def complete_generation(self, decision_id: str, *, generation: int, state: str) -> ControllerGenerationStatus:
        self.completed.append((decision_id, generation, state))
        return ControllerGenerationStatus(
            self.status_value.decision_id,
            Generation(generation),
            "program-lineage",
            ControllerGenerationState(state),
        )


def _runner_status() -> ProgramControllerDecisionStatus:
    return ProgramControllerDecisionStatus(
        ControllerDecisionId("decision/program/program/checkpoint/start"),
        ProgramId("program"),
        ProgramEventKind.CHECKPOINT,
        "start",
        ControllerDecisionState.AWAITING_CLAIM,
        0,
        Generation(1),
        2,
        0,
        None,
        None,
        None,
        None,
        None,
        "9999-12-31T23:59:59Z",
        {"program_revision": 0},
    )


def test_program_generation_acknowledges_and_closes_its_ephemeral_lineage(tmp_path: Path) -> None:
    status = _runner_status()
    claim = ControllerDecisionClaim(
        status.decision_id,
        Generation(1),
        ControllerClaimantKind.MODEL,
        "program-controller/test",
        1,
        "9999-12-31T23:59:59Z",
        "program-claim-token",
    )
    bundle = ModelFacingProgramControllerActionBundle(
        1,
        status.decision_id,
        status.program_id,
        PLAN_DIGEST,
        Generation(1),
        status.event_kind,
        status.event_key,
        0,
        TRUNK_HEAD,
        (ModelFacingProgramControllerAction(ProgramControllerActionKind.ACKNOWLEDGE_ONLY),),
        "close this event",
    )
    client = _ProgramClient(status, bundle)
    assert client.claim_value == claim
    adapter = _ProgramAdapter(bundle)
    result = ProgramControllerGenerationRunner(
        client,
        adapter,  # type: ignore[arg-type]
        decision_id=status.decision_id,
        claimant_id=claim.claimant_id,
        cwd=tmp_path,
        model="gpt-test",
    ).run()
    assert result.outcome == "acknowledged"
    assert client.completed == [(str(status.decision_id), 1, ControllerGenerationState.COMPLETED.value)]


def test_program_recovery_replays_committed_action_without_an_sdk_read() -> None:
    class RecoveryClient:
        def __init__(self) -> None:
            self.status_value = _runner_status()
            self.status_value = ProgramControllerDecisionStatus(
                self.status_value.decision_id,
                self.status_value.program_id,
                self.status_value.event_kind,
                self.status_value.event_key,
                ControllerDecisionState.ACTION_COMMITTED,
                2,
                Generation(1),
                2,
                1,
                ControllerClaimantKind.MODEL,
                "program-controller/test",
                None,
                "program-action/committed",
                "f" * 64,
                self.status_value.deadline,
                self.status_value.payload,
            )
            self.reads = 0

        def program_status(self, _decision_id: str) -> ProgramControllerDecisionStatus:
            return self.status_value

        def acknowledge_program_recovered(
            self, _decision_id: str, *, action_id: str, bundle_sha256: str, committed_revision: int
        ) -> ControllerActionReceipt:
            assert action_id == "program-action/committed"
            assert bundle_sha256 == "f" * 64
            assert committed_revision == 2
            return ControllerActionReceipt(
                action_id,
                self.status_value.decision_id,
                Generation(1),
                1,
                bundle_sha256,
                {"replayed": True},
                "acknowledged",
                "2026-09-02T00:00:00Z",
                "2026-09-02T00:00:00Z",
            )

    client = RecoveryClient()

    class NoReadAdapter:
        def inspect_persisted_thread(self, _thread: ThreadIdentity) -> object:
            client.reads += 1
            raise AssertionError("committed recovery must not inspect the SDK thread")

    result = ProgramControllerGenerationRecovery(client, NoReadAdapter()).recover(
        "decision/program/program/checkpoint/start"
    )  # type: ignore[arg-type]
    assert result.outcome == "acknowledged"
    assert client.reads == 0


def test_program_command_group_exposes_register_start_status_and_decisions() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["program", "--help"])
    assert result.exit_code == 0, result.stdout
    for command in ("register", "start", "status", "decisions", "decide"):
        assert command in result.stdout
