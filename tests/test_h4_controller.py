from __future__ import annotations

from pathlib import Path

from codex_flow.config import load_workflow_config
from codex_flow.controller import H4WalkingSkeleton
from codex_flow.domain import (
    Budget,
    BudgetExhaustion,
    DecisionRequest,
    DecisionResponse,
    DispatchId,
    FindingCausalClass,
    RecoveryDecision,
    RecoveryOutcome,
    RepairRecord,
    ReviewFinding,
    ReviewResult,
    RoleId,
    Severity,
    TerminalFailureAfterIdentity,
)
from codex_flow.ledger import Ledger


def test_workflow_toml_has_typed_routes_and_fail_closed_limits() -> None:
    config = load_workflow_config(Path(__file__).parents[1] / "workflow.toml")
    assert config.route("executor").model == "gpt-5.6-luna"
    assert config.route("code-reviewer").reasoning_effort.value == "xhigh"
    assert config.limits.budget().max_repairs == 1
    assert Budget(max_turns=0).exhausted() is BudgetExhaustion.TURNS


def test_recovery_outcomes_are_mutually_distinct() -> None:
    assert H4WalkingSkeleton.classify_recovery().outcome is RecoveryOutcome.CONTINUE_WITH_REPLAN
    assert H4WalkingSkeleton.classify_recovery(underdetermined=True).outcome is RecoveryOutcome.NEEDS_DECISION
    assert (
        H4WalkingSkeleton.classify_recovery(external_prerequisite="credentials").outcome
        is RecoveryOutcome.EXTERNAL_BLOCKED
    )
    assert H4WalkingSkeleton.classify_recovery(proven_infeasible=True).outcome is RecoveryOutcome.FAILED


def test_decision_recovery_and_budget_facts_survive_ledger_reopen(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    ledger.create_run("facts")
    ledger.create_milestone("facts", "m")
    ledger.record_h4_decision_request("facts", "m", DecisionRequest("decision-1", "choose a safe route", ("a", "b")))
    ledger.record_h4_decision_response("facts", "m", DecisionResponse("decision-1", "a", "the contract selects a"))
    ledger.record_h4_recovery(
        "facts",
        "m",
        RecoveryDecision(RecoveryOutcome.CONTINUE_WITH_REPLAN, "same accepted boundary", "checkpoint-1"),
    )
    ledger.record_h4_budget("facts", "m", Budget(max_turns=2, max_repairs=1), BudgetExhaustion.REPAIRS)
    ledger.close()
    ledger.reopen()
    kinds = [record.kind for record in ledger.h4_lifecycle("facts", "m")]
    assert kinds == ["decision_request", "decision_response", "recovery_decision", "budget_snapshot"]


def test_objective_rejection_same_owner_repair_fresh_acceptance_and_artifact(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("pilot")
    ledger.create_milestone("pilot", "objective")
    target = root / "authorized.txt"
    revisions = iter(("before", "after"))
    calls: list[tuple[str, str]] = []
    finding = ReviewFinding(
        "F-objective",
        FindingCausalClass.IMPLEMENTATION,
        Severity.P1,
        True,
        "the objective file must contain the repaired marker",
        {"path": "authorized.txt", "observed": "missing"},
        "objective marker is present",
    )

    def reviewer(revision: str, fresh: bool) -> ReviewResult:
        assert fresh is True
        calls.append(("review", revision))
        if len(calls) == 1:
            return ReviewResult("review-1", RoleId("code-reviewer"), False, (finding,), revision)
        assert revision == "after"
        return ReviewResult("review-2", RoleId("code-reviewer"), True, (), revision, prior_review_id="review-1")

    def repairer(review_finding: ReviewFinding) -> RepairRecord:
        assert review_finding.finding_id == finding.finding_id
        target.write_text("repaired\n", encoding="utf-8")
        calls.append(("repair", review_finding.finding_id))
        return RepairRecord(
            "repair-1",
            review_finding.finding_id,
            DispatchId("pilot/objective/executor/1"),
            "write objective marker",
            "repaired",
        )

    result = H4WalkingSkeleton(ledger, root).run(
        "pilot",
        "objective",
        objective=lambda: {"edited": target.write_text("initial\n", encoding="utf-8") is None},
        reviewer=reviewer,
        repair=repairer,
        current_revision=lambda: next(revisions),
        budget=Budget(max_turns=2, max_repairs=1, max_reviews=2, max_validations=2),
    )
    assert result.accepted is True
    assert result.status == "accepted"
    assert calls == [("review", "before"), ("repair", "F-objective"), ("review", "after")]
    assert ledger.current_state("pilot", "objective").value == "ACCEPTED"
    assert [record.kind for record in ledger.h4_lifecycle("pilot", "objective")] == [
        "objective_completed",
        "review_started",
        "review_rejected",
        "repair_completed",
        "repair_execution_completed",
        "review_started",
        "review_accepted",
    ]
    artifact = root / ".codex-flow" / "runs" / "pilot" / "milestones" / "objective" / "review.json"
    assert artifact.is_file()
    assert '"same_owner_repair":true' in artifact.read_text(encoding="utf-8")


def test_stale_review_is_not_relabelled_as_acceptance(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("stale")
    ledger.create_milestone("stale", "review")
    finding = ReviewFinding(
        "F-stale",
        FindingCausalClass.STALE_REVIEW,
        Severity.P2,
        True,
        "review revision is not current",
        {"expected": "new"},
        "review must cover current revision",
    )
    result = H4WalkingSkeleton(ledger, root).run(
        "stale",
        "review",
        objective=lambda: {"ok": True},
        reviewer=lambda revision, fresh: ReviewResult(
            "stale-review", RoleId("code-reviewer"), False, (finding,), "old"
        ),
        repair=lambda item: RepairRecord(
            "never",
            item.finding_id,
            DispatchId("stale/review/executor/1"),
            "unused",
            "unused",
        ),
        current_revision=lambda: "new",
    )
    assert result.status == "stale_review"
    assert result.accepted is False
    assert result.recovery is not None
    assert result.recovery.outcome is RecoveryOutcome.CONTINUE_WITH_REPLAN


def test_exhausted_limit_is_distinct_from_external_block(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    ledger = Ledger(root / ".codex-flow" / "workflow.db")
    ledger.create_run("limit")
    ledger.create_milestone("limit", "m")
    result = H4WalkingSkeleton(ledger, root).run(
        "limit",
        "m",
        objective=lambda: {"unreachable": True},
        reviewer=lambda revision, fresh: (_ for _ in ()).throw(AssertionError("review must not run")),
        repair=lambda finding: (_ for _ in ()).throw(AssertionError("repair must not run")),
        current_revision=lambda: "unused",
        budget=Budget(max_turns=0),
    )
    assert result.status.value == "limit_exhausted"
    assert result.recovery is not None
    assert result.recovery.outcome is RecoveryOutcome.FAILED


def test_transport_failure_and_objective_failure_are_distinct(tmp_path: Path) -> None:
    for run_id, error, status in (
        ("transport", TerminalFailureAfterIdentity("opaque"), "transport_failure"),
        ("failed", RuntimeError("implementation error"), "failed"),
    ):
        root = tmp_path / run_id
        root.mkdir()
        ledger = Ledger(root / ".codex-flow" / "workflow.db")
        ledger.create_run(run_id)
        ledger.create_milestone(run_id, "m")
        result = H4WalkingSkeleton(ledger, root).run(
            run_id,
            "m",
            objective=lambda error=error: (_ for _ in ()).throw(error),
            reviewer=lambda revision, fresh: (_ for _ in ()).throw(AssertionError("review must not run")),
            repair=lambda finding: (_ for _ in ()).throw(AssertionError("repair must not run")),
            current_revision=lambda: "unused",
        )
        assert result.status.value == status
