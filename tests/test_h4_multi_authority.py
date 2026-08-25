from __future__ import annotations

from pathlib import Path

from codex_flow.config import AuthorityUnavailable, WorkflowConfig, load_workflow_config
from codex_flow.controller import H4WalkingSkeleton
from codex_flow.domain import (
    AcceptanceMode,
    Budget,
    DecisionRequest,
    DecisionResponse,
    DispatchId,
    FindingCausalClass,
    RenderedEvidence,
    RepairRecord,
    ReplanProposal,
    ReviewFinding,
    ReviewResult,
    RoleId,
    Severity,
)
from codex_flow.ledger import Ledger


def test_all_modes_require_distinct_authorities_and_fixed_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("multi")
    ledger.create_milestone("multi", "m")
    target = root / "target.txt"
    render = root / "render.svg"
    render.write_text("<svg/>\n", encoding="utf-8")
    first_objective = ReviewFinding(
        "F-objective",
        FindingCausalClass.IMPLEMENTATION,
        Severity.P1,
        True,
        "repair the target marker",
        {"path": "target.txt"},
        "target has the repaired marker",
    )
    first_architecture = ReviewFinding(
        "F-architecture",
        FindingCausalClass.ACCEPTANCE,
        Severity.P1,
        True,
        "accept the revised strategy",
        {"strategy": "initial"},
        "architecture accepts the revised strategy",
    )
    review_round = {"objective": 0, "visual": 0, "architecture": 0}

    def objective() -> dict[str, object]:
        target.write_text("needs-repair\n", encoding="utf-8")
        return {"edited": True}

    def revision() -> str:
        return target.read_text(encoding="utf-8").strip()

    def evidence(token: str) -> RenderedEvidence:
        return RenderedEvidence("render-1", token, "0" * 64, "render.svg", width=1, height=1)

    def objective_reviewer(token: str, fresh: bool) -> ReviewResult:
        review_round["objective"] += 1
        if review_round["objective"] == 1:
            return ReviewResult(
                "objective-1",
                RoleId("code-reviewer"),
                False,
                (first_objective,),
                token,
                acceptance_mode=AcceptanceMode.OBJECTIVE,
            )
        return ReviewResult(
            "objective-2",
            RoleId("code-reviewer"),
            True,
            (),
            token,
            prior_review_id="objective-1",
            acceptance_mode=AcceptanceMode.OBJECTIVE,
        )

    def visual_reviewer(token: str, fixed: RenderedEvidence, fresh: bool) -> ReviewResult:
        review_round["visual"] += 1
        return ReviewResult(
            f"visual-{review_round['visual']}",
            RoleId("visual-reviewer"),
            True,
            (),
            token,
            evidence_ids=(fixed.evidence_id,),
            acceptance_mode=AcceptanceMode.VISUAL,
        )

    def architecture_reviewer(token: str, fixed: RenderedEvidence, fresh: bool) -> ReviewResult:
        review_round["architecture"] += 1
        if review_round["architecture"] == 1:
            return ReviewResult(
                "architecture-1",
                RoleId("architecture-reviewer"),
                False,
                (first_architecture,),
                token,
                acceptance_mode=AcceptanceMode.ARCHITECTURE,
            )
        return ReviewResult(
            "architecture-2",
            RoleId("architecture-reviewer"),
            True,
            (),
            token,
            prior_review_id="architecture-1",
            acceptance_mode=AcceptanceMode.ARCHITECTURE,
        )

    def repair(finding: ReviewFinding) -> RepairRecord:
        assert finding.finding_id == first_objective.finding_id
        target.write_text("repaired\n", encoding="utf-8")
        return RepairRecord(
            "repair-1",
            finding.finding_id,
            DispatchId("multi/m/executor/1"),
            "write repaired marker",
            "repaired",
        )

    result = H4WalkingSkeleton(
        ledger, root, load_workflow_config(Path(__file__).parents[1] / "workflow.toml")
    ).run_multi_authority(
        "multi",
        "m",
        objective=objective,
        repair=repair,
        current_revision=revision,
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL, AcceptanceMode.ARCHITECTURE),
        objective_reviewer=objective_reviewer,
        visual_reviewer=visual_reviewer,
        architecture_reviewer=architecture_reviewer,
        render_evidence=evidence,
        architecture_replan=lambda finding: ReplanProposal("bounded revised strategy"),
        budget=Budget(max_turns=2, max_repairs=1, max_reviews=2, max_validations=2),
    )
    assert result.accepted is True
    assert result.authority_plan is not None
    assert len(result.authority_plan.assignments) == 6
    assert result.rendered_evidence[0].evidence_id == "render-1"
    assert "architecture_replan_accepted" in [item.kind for item in result.lifecycle]
    assert ledger.current_state("multi", "m").value == "ACCEPTED"


def test_route_alias_fails_closed_before_claim(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("alias")
    ledger.create_milestone("alias", "m")
    config = load_workflow_config(Path(__file__).parents[1] / "workflow.toml")
    routes = dict(config.roles)
    routes[RoleId("visual-reviewer")] = routes[RoleId("code-reviewer")]
    bad = WorkflowConfig(config.version, routes, config.limits)
    try:
        bad.derive_authorities((AcceptanceMode.OBJECTIVE, AcceptanceMode.VISUAL))
    except AuthorityUnavailable:
        pass
    else:
        raise AssertionError("aliased role must fail closed")


def test_notification_failure_is_non_authoritative_and_planner_decision_is_durable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("decision")
    ledger.create_milestone("decision", "m")
    finding = ReviewFinding(
        "F-contract",
        FindingCausalClass.CONTRACT,
        Severity.P1,
        True,
        "the proposed strategy changes an accepted boundary",
        {"boundary": "contract"},
        "architecture strategy stays within the accepted contract",
    )

    def accepted_objective(token: str, fresh: bool) -> ReviewResult:
        return ReviewResult(
            "objective", RoleId("code-reviewer"), True, (), token, acceptance_mode=AcceptanceMode.OBJECTIVE
        )

    architecture_calls = 0

    def rejected_architecture(token: str, fresh: bool) -> ReviewResult:
        nonlocal architecture_calls
        architecture_calls += 1
        return ReviewResult(
            f"architecture-{architecture_calls}",
            RoleId("architecture-reviewer"),
            False,
            (finding,),
            token,
            acceptance_mode=AcceptanceMode.ARCHITECTURE,
        )

    def planner(request: DecisionRequest) -> DecisionResponse:
        assert request.request_id == "architecture-replan-decision"
        return DecisionResponse(
            "architecture-replan-decision", "request-user-decision", "boundary change needs user authority"
        )

    result = H4WalkingSkeleton(
        ledger, root, load_workflow_config(Path(__file__).parents[1] / "workflow.toml")
    ).run_multi_authority(
        "decision",
        "m",
        objective=lambda: {"ok": True},
        repair=lambda item: (_ for _ in ()).throw(AssertionError("repair must not run")),
        current_revision=lambda: "revision",
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
        objective_reviewer=accepted_objective,
        architecture_reviewer=rejected_architecture,
        architecture_replan=lambda item: ReplanProposal("boundary-changing strategy", contract_unchanged=False),
        planner=planner,
        budget=Budget(max_turns=1, max_repairs=1, max_reviews=2),
    )
    assert result.status.value == "needs_decision"
    assert result.accepted is False
    assert [item.kind for item in result.lifecycle][-3:] == [
        "decision_request",
        "decision_response",
        "recovery_decision",
    ]


def test_notification_failure_does_not_relabel_acceptance(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("notify")
    ledger.create_milestone("notify", "m")

    def reviewer(token: str, fresh: bool) -> ReviewResult:
        return ReviewResult("review", RoleId("code-reviewer"), True, (), token)

    result = H4WalkingSkeleton(
        ledger, root, load_workflow_config(Path(__file__).parents[1] / "workflow.toml")
    ).run_multi_authority(
        "notify",
        "m",
        objective=lambda: {"ok": True},
        repair=lambda item: (_ for _ in ()).throw(AssertionError("repair must not run")),
        current_revision=lambda: "revision",
        objective_reviewer=reviewer,
        notification=lambda _: (_ for _ in ()).throw(RuntimeError("delivery unavailable")),
        budget=Budget(max_turns=1, max_repairs=1, max_reviews=2),
    )
    assert result.accepted is True
    assert result.status.value == "accepted"
    assert result.notification_failure is True
    assert result.lifecycle[-1].kind == "notification_failure"
