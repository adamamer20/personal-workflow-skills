from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codex_flow.cli as cli_module
from codex_flow.backends.codex_sdk import _provider_output_schema
from codex_flow.cli import app
from codex_flow.contracts import (
    ModelFacingProgramControllerAction,
    ModelFacingProgramControllerActionBundle,
    model_facing_program_controller_action_schema,
)
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
    ProgramControllerContext,
    ProgramControllerDecisionStatus,
    ProgramControllerNodeContext,
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
    validate_output_schema,
    validate_structured_output,
)
from codex_flow.ledger import Ledger, StaleWriter
from codex_flow.program_controller import ProgramControllerGenerationRecovery, ProgramControllerGenerationRunner
from codex_flow.supervisor import Supervisor
from codex_flow.worktrees import WorkspaceConflict, WorktreeError, WorktreeManager

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
    for effect_id, _milestone_id in bundle.external_effects:
        ledger.record_program_action_effect(bundle.action_id, effect_id, state="applied")
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
    with pytest.raises(ValueError, match="concurrent owners"):
        ProgramGraph(
            ProgramId("program"),
            tmp_path / "plan.md",
            PLAN_DIGEST,
            (ProgramNodeSpec("first", first), ProgramNodeSpec("second", second)),
            TRUNK_HEAD,
        )

    serial = ProgramGraph(
        ProgramId("program"),
        tmp_path / "plan.md",
        PLAN_DIGEST,
        (
            ProgramNodeSpec("first", first),
            ProgramNodeSpec("second", second, (MilestoneId("first"),)),
        ),
        TRUNK_HEAD,
    )
    assert serial.node("second").mutable_surfaces == ("src/shared.py",)

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


def test_program_executor_terminal_replay_is_idempotent_and_dispatch_bound(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(_graph(tmp_path))
        start = ledger.start_program("program")
        _claim_and_apply(
            ledger,
            start,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_READY_MILESTONES,
                milestone_ids=("first",),
            ),
        )
        dispatch_id = "program/first/executor/1"
        first = ledger.record_program_executor_result(
            "program",
            "first",
            candidate_sha=None,
            terminal_status="failed",
            dispatch_id=dispatch_id,
            result_sha256="1" * 64,
        )
        replayed = ledger.record_program_executor_result(
            "program",
            "first",
            candidate_sha=None,
            terminal_status="failed",
            dispatch_id=dispatch_id,
            result_sha256="1" * 64,
        )
        assert replayed.revision == first.revision
        assert ledger.program_executor_terminal_recorded(
            "program",
            "first",
            dispatch_id=dispatch_id,
            terminal_status="failed",
            result_sha256="1" * 64,
        )
        terminal_facts = [
            fact for fact in ledger.review_lifecycle("program", "first") if fact.kind == "executor_terminal"
        ]
        assert len(terminal_facts) == 1
        decisions = [
            item
            for item in ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.CONTROLLER_ATTENTION
        ]
        assert len(decisions) == 1
        assert dispatch_id in decisions[0].event_key
        with pytest.raises(StaleWriter, match="terminal replay conflicts"):
            ledger.record_program_executor_result(
                "program",
                "first",
                candidate_sha=CANDIDATE_SHA,
                terminal_status="completed",
                dispatch_id=dispatch_id,
                result_sha256="2" * 64,
            )
    finally:
        ledger.close()


def test_program_context_contains_exact_graph_and_rejects_stale_revision(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(_graph(tmp_path))
        context = ledger.program_controller_context(
            "program",
            expected_revision=0,
            expected_trunk_head=TRUNK_HEAD,
        )
        assert context.plan_path == tmp_path / "canonical-plan.md"
        assert [str(node.milestone_id) for node in context.nodes] == ["first", "second"]
        assert context.nodes[0].ready is True
        assert context.nodes[1].dependencies == (MilestoneId("first"),)
        assert context.nodes[0].mutable_surfaces == ("src/first.py",)
        assert context.nodes[0].review_roles == ("architecture-reviewer", "code-reviewer")
        ledger.start_program("program")
        with pytest.raises(StaleWriter, match="context identity is stale"):
            ledger.program_controller_context(
                "program",
                expected_revision=0,
                expected_trunk_head=TRUNK_HEAD,
            )
    finally:
        ledger.close()


def test_program_context_ipc_projection_is_typed_and_revision_bound(tmp_path: Path) -> None:
    supervisor = Supervisor(tmp_path)
    try:
        supervisor.ledger.register_program(_graph(tmp_path))
        response = supervisor._controller_request(
            {
                "version": 1,
                "operation": "program_context",
                "program_id": "program",
                "expected_revision": 0,
                "expected_trunk_head": TRUNK_HEAD,
            }
        )
        context = ProgramControllerContext.from_json(response["context"])  # type: ignore[arg-type]
        assert context.program_revision == 0
        assert context.nodes[1].dependencies == (MilestoneId("first"),)
        supervisor.ledger.start_program("program")
        with pytest.raises(StaleWriter, match="context identity is stale"):
            supervisor._controller_request(
                {
                    "version": 1,
                    "operation": "program_context",
                    "program_id": "program",
                    "expected_revision": 0,
                    "expected_trunk_head": TRUNK_HEAD,
                }
            )
    finally:
        supervisor.close()


def test_program_revision_allows_only_one_claim_and_coalesces_stale_events(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    try:
        ledger.register_program(_graph(tmp_path))
        first = ledger.record_program_event("program", ProgramEventKind.CHECKPOINT, "first-event")
        second = ledger.record_program_event("program", ProgramEventKind.CHECKPOINT, "second-event")
        claim = ledger.claim_program_controller_decision(
            first.decision_id,
            claimant_id="program-controller/first",
            expected_revision=first.revision,
            generation=1,
        )
        with pytest.raises(StaleWriter, match="another controller generation owns"):
            ledger.claim_program_controller_decision(
                second.decision_id,
                claimant_id="program-controller/second",
                expected_revision=second.revision,
                generation=1,
            )
        bundle = _bundle(
            first,
            claim,
            ModelFacingProgramControllerAction(ProgramControllerActionKind.ACKNOWLEDGE_ONLY),
        )
        ledger.submit_program_controller_actions(bundle, claimant_id=claim.claimant_id, token=str(claim.token))
        assert ledger.reconcile_stale_program_controller_decisions() == 1
        assert ledger.program_controller_decision(second.decision_id).state is ControllerDecisionState.SUPERSEDED
        coalesced = [item for item in ledger.program_controller_decisions() if item.event_key == "revision/1/coalesced"]
        assert len(coalesced) == 1
        assert coalesced[0].payload["program_revision"] == 1
    finally:
        ledger.close()


def test_supervisor_schedules_only_one_program_controller_child_per_program_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(tmp_path)
    try:
        supervisor.ledger.register_program(_graph(tmp_path))
        supervisor.ledger.record_program_event("program", ProgramEventKind.CHECKPOINT, "first-event")
        supervisor.ledger.record_program_event("program", ProgramEventKind.CHECKPOINT, "second-event")
        supervisor.epoch = 1
        spawned: list[str] = []

        def spawn(status: ProgramControllerDecisionStatus, *, recovery: bool = False) -> bool:
            assert recovery is False
            spawned.append(str(status.decision_id))
            return True

        monkeypatch.setattr(supervisor, "_spawn_program_controller_generation", spawn)
        assert supervisor._schedule_program_controller_generations() is True
        assert len(spawned) == 1
    finally:
        supervisor.close()


def test_program_partial_external_effect_failure_is_durable_and_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = ProgramGraph(
        ProgramId("program"),
        tmp_path / "canonical-plan.md",
        PLAN_DIGEST,
        (
            ProgramNodeSpec("first", _capsule(tmp_path, "first", "src/first.py")),
            ProgramNodeSpec("second", _capsule(tmp_path, "second", "src/second.py")),
        ),
        TRUNK_HEAD,
    )
    supervisor = Supervisor(tmp_path)
    try:
        supervisor.ledger.register_program(graph)
        decision = supervisor.ledger.start_program("program")
        claim = supervisor.ledger.claim_program_controller_decision(
            decision.decision_id,
            claimant_id="program-controller/test",
            expected_revision=decision.revision,
            generation=1,
        )
        bundle = _bundle(
            decision,
            claim,
            ModelFacingProgramControllerAction(
                ProgramControllerActionKind.START_READY_MILESTONES,
                milestone_ids=("first", "second"),
            ),
        )
        receipt = supervisor.ledger.submit_program_controller_actions(
            bundle,
            claimant_id=claim.claimant_id,
            token=str(claim.token),
        )
        calls: list[str] = []

        def enqueue(*, milestone_id: str, **_facts: object) -> None:
            calls.append(milestone_id)
            if milestone_id == "second":
                raise RuntimeError("bounded enqueue failure")

        monkeypatch.setattr(supervisor, "_enqueue_program_worker", enqueue)
        with pytest.raises(RuntimeError, match="bounded enqueue failure"):
            supervisor._apply_program_action_bundle(bundle)
        assert calls == ["first", "second"]
        assert supervisor.ledger.program_action_effect_state(bundle.action_id, "start:first") == "applied"
        assert supervisor.ledger.program_action_effect_state(bundle.action_id, "start:second") == "failed"
        status = supervisor.ledger.program_controller_decision(decision.decision_id)
        assert status.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
        with pytest.raises(StaleWriter, match="unapplied external effects"):
            supervisor.ledger.acknowledge_controller_action(
                decision.decision_id,
                action_id=receipt.action_id,
                bundle_sha256=bundle.sha256,
                committed_revision=receipt.expected_revision + 1,
                claimant_id=claim.claimant_id,
                token=str(claim.token),
            )
    finally:
        supervisor.close()

    restarted = Supervisor(tmp_path)
    try:
        replayed: list[str] = []
        monkeypatch.setattr(
            restarted,
            "_enqueue_program_worker",
            lambda *, milestone_id, **_facts: replayed.append(milestone_id),
        )
        assert restarted._reconcile_program_action_outboxes() is False
        assert replayed == []
        attention = [
            item
            for item in restarted.ledger.program_controller_decisions()
            if item.event_kind is ProgramEventKind.CONTROLLER_ATTENTION
            and item.payload.get("payload", {}).get("effect_id") == "start:second"  # type: ignore[union-attr]
        ]
        assert len(attention) == 1
    finally:
        restarted.close()


class _ProgramAdapter:
    def __init__(self, bundle: ModelFacingProgramControllerActionBundle) -> None:
        self.bundle = bundle
        self.thread = ThreadIdentity("program-controller-thread")
        self.bound_turns: list[str] = []
        self.prompts: list[str] = []

    def start_thread(self) -> ThreadIdentity:
        return self.thread

    def run_turn(
        self, _thread: ThreadIdentity, _prompt: str, *, output_schema: object, turn_callback: object
    ) -> TurnObservation:
        assert output_schema is not None
        assert callable(turn_callback)
        self.prompts.append(_prompt)
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

    def program_context(
        self,
        program_id: str,
        *,
        expected_revision: int,
        expected_trunk_head: str,
    ) -> ProgramControllerContext:
        assert program_id == str(self.status_value.program_id)
        assert expected_revision == self.status_value.payload["program_revision"]
        assert expected_trunk_head == self.status_value.payload["trunk_head"]
        return _runner_context()

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
        {"program_revision": 0, "plan_digest": PLAN_DIGEST, "trunk_head": TRUNK_HEAD},
    )


def _runner_context() -> ProgramControllerContext:
    return ProgramControllerContext(
        ProgramId("program"),
        0,
        PLAN_DIGEST,
        Path("/tmp/codex-flow-program-plan.md"),
        TRUNK_HEAD,
        (
            ProgramControllerNodeContext(
                MilestoneId("first"),
                WorkflowState.PLANNED.value,
                (),
                ("src/first.py",),
                (AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
                ("architecture-reviewer", "code-reviewer"),
                None,
                (),
                (),
                False,
                True,
            ),
        ),
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
    assert '"mutable_surfaces":["src/first.py"]' in adapter.prompts[0]
    assert '"review_roles":["architecture-reviewer","code-reviewer"]' in adapter.prompts[0]


def test_program_action_schema_is_locally_closed_and_provider_projectable() -> None:
    schema = model_facing_program_controller_action_schema()
    validate_output_schema(schema)
    projected = _provider_output_schema(schema)
    assert projected is not None
    actions = projected["properties"]["actions"]  # type: ignore[index]
    assert isinstance(actions, dict)
    items = actions["items"]
    assert isinstance(items, dict)
    branches = items["anyOf"]
    assert isinstance(branches, list)
    assert len(branches) == len(ProgramControllerActionKind)
    assert all(branch["additionalProperties"] is False for branch in branches)

    status = _runner_status()
    sample_actions = (
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.START_READY_MILESTONES,
            milestone_ids=("first",),
        ),
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.START_REVIEWS,
            milestone_id="first",
            candidate_sha=CANDIDATE_SHA,
            review_roles=("code-reviewer",),
        ),
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.REQUEST_REPAIR,
            milestone_id="first",
            candidate_sha=CANDIDATE_SHA,
            finding_ids=("finding",),
        ),
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.PROMOTE_CANDIDATE,
            milestone_id="first",
            candidate_sha=CANDIDATE_SHA,
            review_ids=("review",),
        ),
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.INTEGRATE_CANDIDATE,
            milestone_id="first",
            candidate_sha=CANDIDATE_SHA,
            expected_trunk_head=TRUNK_HEAD,
            integration_strategy="merge",
        ),
        ModelFacingProgramControllerAction(ProgramControllerActionKind.REQUIRE_REPLAN, reason="replan"),
        ModelFacingProgramControllerAction(
            ProgramControllerActionKind.REQUIRE_HUMAN_ATTENTION,
            reason="human authority required",
        ),
        ModelFacingProgramControllerAction(ProgramControllerActionKind.ACKNOWLEDGE_ONLY),
    )
    for action in sample_actions:
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
            (action,),
            "close this event",
        )
        validate_structured_output(bundle.to_json(), schema)
    malformed = bundle.to_json()
    malformed["actions"] = [{"kind": "acknowledge_only", "reason": "not allowed"}]
    with pytest.raises(ValueError, match="must match exactly one oneOf branch"):
        validate_structured_output(malformed, schema)


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


def test_program_decisions_uses_program_decision_ipc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Client:
        def pending(self) -> object:
            raise AssertionError("dispatch-local pending endpoint must not be used")

        def program_pending(self) -> tuple[ProgramControllerDecisionStatus, ...]:
            return (_runner_status(),)

    monkeypatch.setattr(cli_module, "_controller_decision_client", lambda _state_root: Client())
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app,
        ["program", "decisions", "--state-root", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.stdout
    assert '"program_id":"program"' in result.stdout


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _integration_repositories(tmp_path: Path, strategy: str) -> tuple[Path, Path, str, str]:
    repository = tmp_path / "repository"
    candidate = tmp_path / "candidate"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Codex Flow Test")
    _git(repository, "config", "user.email", "codex-flow@example.invalid")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "worktree", "add", "-b", "agent/candidate", str(candidate), base)
    (candidate / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(candidate, "add", "candidate.txt")
    _git(candidate, "commit", "-m", "candidate")
    candidate_sha = _git(candidate, "rev-parse", "HEAD")
    if strategy != "fast_forward":
        (repository / "trunk.txt").write_text("trunk\n", encoding="utf-8")
        _git(repository, "add", "trunk.txt")
        _git(repository, "commit", "-m", "trunk")
    return repository, candidate, _git(repository, "rev-parse", "HEAD"), candidate_sha


@pytest.mark.parametrize("strategy", ["fast_forward", "merge", "cherry_pick"])
def test_program_git_integration_recovers_exact_applied_effect_after_receipt_crash(
    tmp_path: Path, strategy: str
) -> None:
    repository, candidate, expected, candidate_sha = _integration_repositories(tmp_path, strategy)
    manager = WorktreeManager()
    first = manager.integrate_candidate(
        repository_root=repository,
        candidate_workspace=candidate,
        candidate_branch="agent/candidate",
        candidate_sha=candidate_sha,
        expected_trunk_head=expected,
        strategy=strategy,
    )
    recovered = manager.integrate_candidate(
        repository_root=repository,
        candidate_workspace=candidate,
        candidate_branch="agent/candidate",
        candidate_sha=candidate_sha,
        expected_trunk_head=expected,
        strategy=strategy,
    )
    assert recovered["recovered"] is True
    assert recovered["before_trunk_head"] == expected
    assert recovered["after_trunk_head"] == first["after_trunk_head"]


def test_program_git_integration_recovery_rejects_unknown_trunk_advance(tmp_path: Path) -> None:
    repository, candidate, expected, candidate_sha = _integration_repositories(tmp_path, "merge")
    (repository / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-m", "unrelated")
    with pytest.raises(WorkspaceConflict, match="trunk HEAD changed"):
        WorktreeManager().integrate_candidate(
            repository_root=repository,
            candidate_workspace=candidate,
            candidate_branch="agent/candidate",
            candidate_sha=candidate_sha,
            expected_trunk_head=expected,
            strategy="merge",
        )


@pytest.mark.parametrize("alias_target", ["repository", "candidate"])
def test_program_git_integration_rejects_symlinked_authority_paths(tmp_path: Path, alias_target: str) -> None:
    repository, candidate, expected, candidate_sha = _integration_repositories(tmp_path, "fast_forward")
    target = repository if alias_target == "repository" else candidate
    alias = tmp_path / f"{alias_target}-alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(WorktreeError, match="integration path must not traverse symlinks"):
        WorktreeManager().integrate_candidate(
            repository_root=alias if alias_target == "repository" else repository,
            candidate_workspace=alias if alias_target == "candidate" else candidate,
            candidate_branch="agent/candidate",
            candidate_sha=candidate_sha,
            expected_trunk_head=expected,
            strategy="fast_forward",
        )
