from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_flow.backends.codex_sdk import ControllerThreadInspection, ControllerThreadInspectionKind
from codex_flow.contracts import ModelFacingControllerAction, ModelFacingControllerActionBundle
from codex_flow.controller_recovery import ControllerGenerationRecovery, ControllerGenerationRunner
from codex_flow.domain import (
    ControllerActionKind,
    ControllerClaimantKind,
    ControllerDecisionState,
    ControllerGenerationState,
    DispatchId,
    RetryFailureClass,
    TemporaryRateLimitAfterIdentity,
    ThreadIdentity,
    TurnObservation,
)
from codex_flow.ledger import CorruptSchemaError, Ledger, RecordNotFound, StaleWriter


def _queue(ledger: Ledger, root: Path) -> str:
    ledger.create_run("recovery")
    ledger.create_milestone("recovery", "m")
    ledger.claim_dispatch("recovery", "m", "executor", 1)
    dispatch = str(DispatchId.from_parts("recovery", "m", "executor", 1))
    ledger.enqueue_dispatch(
        dispatch,
        backend="sdk_headless",
        capsule_json='{"model":"test","prompt":"bounded"}\n',
        route_json=json.dumps(
            {
                "effective_permission": {
                    "mode": "read_only",
                    "sandbox_mode": "read-only",
                    "approval_policy": "never",
                    "monotonic": True,
                },
                "leaf_worker_policy": {"agents.enabled": False, "features.multi_agent": False, "config_overrides": []},
                "native_profile_sha256": None,
                "native_compatibility_sha256": None,
            },
            sort_keys=True,
        ),
        workspace_path=root,
        result_contract_sha256="0" * 64,
        source_thread_id="source-thread",
    )
    return dispatch


def test_controller_decision_claim_commit_ack_is_idempotent(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path)
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=decision.revision,
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "action-1",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "close",
    )
    receipt = ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
    assert receipt.state == "committed"
    assert ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token)) == receipt
    acknowledged = ledger.acknowledge_controller_action(
        decision.decision_id,
        action_id="action-1",
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
        claimant_id="operator",
        token=str(claim.token),
    )
    assert acknowledged.state == "acknowledged"
    assert ledger.controller_decision(decision.decision_id).state.value == "acknowledged"
    ledger.close()


def test_controller_decision_conflicting_claim_is_closed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path)
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")
    wake = ledger.claim_wake(f"wake/{dispatch}/checkpoint")
    assert wake is not None and wake["state"] == "starting"
    ledger.record_wake_delivery(str(wake["delivery_id"]), outcome="delivered", source_turn_id="source-turn")
    ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.MODEL,
        claimant_id="model",
        expected_revision=ledger.controller_decision(decision.decision_id).revision,
    )
    with pytest.raises(StaleWriter):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=0,
        )
    ledger.close()


def test_controller_rate_limit_defers_same_generation_and_turn_rebind_is_cas_ordered(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    ledger._db().execute(
        "UPDATE controller_decisions SET deadline = '2099-09-01T10:30:00Z' WHERE decision_id = ?",
        (str(decision.decision_id),),
    )
    ledger._db().commit()
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.MODEL,
        claimant_id="controller-generation",
        expected_revision=ledger.controller_decision(decision.decision_id).revision,
        lease_seconds=300,
        now="2099-09-01T10:00:00Z",
    )
    ledger.prepare_controller_generation(decision.decision_id, generation=1, now="2099-09-01T10:00:00Z")
    ledger.bind_controller_generation_thread(
        decision.decision_id,
        generation=1,
        controller_thread_id="controller-thread",
        now="2099-09-01T10:00:00Z",
    )
    ledger.bind_controller_generation_turn(
        decision.decision_id,
        generation=1,
        controller_thread_id="controller-thread",
        controller_turn_id="limit-turn-1",
        now="2099-09-01T10:00:01Z",
    )
    rebound = ledger.bind_controller_generation_turn(
        decision.decision_id,
        generation=1,
        controller_thread_id="controller-thread",
        controller_turn_id="limit-turn-2",
        previous_controller_turn_id="limit-turn-1",
        now="2099-09-01T10:00:02Z",
    )
    assert rebound.controller_turn_id == "limit-turn-2"
    with pytest.raises(StaleWriter, match="retry turn identity is stale"):
        ledger.bind_controller_generation_turn(
            decision.decision_id,
            generation=1,
            controller_thread_id="controller-thread",
            controller_turn_id="stale-turn",
            previous_controller_turn_id="limit-turn-1",
            now="2099-09-01T10:00:03Z",
        )

    deferred = ledger.defer_controller_rate_limit(
        decision.decision_id,
        generation=1,
        claimant_id="controller-generation",
        token=str(claim.token),
        expected_revision=claim.revision,
        retry_at="2099-09-01T13:26:05Z",
        now="2099-09-01T10:00:04Z",
    )
    status = ledger.controller_decision(decision.decision_id)
    assert deferred.revision == claim.revision + 1
    assert deferred.lease_expires_at == "2099-09-01T13:31:05.000000Z"
    assert status.claim_expires_at == deferred.lease_expires_at
    assert status.deadline == "2099-09-01T14:26:05.000000Z"
    assert status.state is ControllerDecisionState.CLAIMED
    assert ledger.controller_generation(decision.decision_id, 1).state is ControllerGenerationState.ACTIVE
    ledger.close()


def test_controller_runner_waits_once_after_three_adapter_retries_then_uses_same_thread(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    waited: list[str] = []
    run_calls = 0

    class Client:
        def status(self, decision_id: str):
            return ledger.controller_decision(decision_id)

        def claim(self, decision_id: str, **kwargs):
            return ledger.claim_controller_decision(
                decision_id,
                claimant_kind=ControllerClaimantKind.MODEL,
                claimant_id=kwargs["claimant_id"],
                expected_revision=kwargs["expected_revision"],
                generation=kwargs["generation"],
            )

        def prepare_generation(self, decision_id: str, **kwargs):
            return ledger.prepare_controller_generation(decision_id, **kwargs)

        def bind_generation_thread(self, decision_id: str, **kwargs):
            return ledger.bind_controller_generation_thread(decision_id, **kwargs)

        def bind_generation_turn(self, decision_id: str, **kwargs):
            return ledger.bind_controller_generation_turn(decision_id, **kwargs)

        def defer_rate_limit(self, decision_id: str, **kwargs):
            return ledger.defer_controller_rate_limit(decision_id, **kwargs)

        def renew_claim(self, decision_id: str, **kwargs):
            return ledger.renew_controller_decision_claim(decision_id, **kwargs)

        def submit_actions(self, bundle, **kwargs):
            return ledger.submit_controller_actions(bundle, **kwargs)

        def acknowledge(self, decision_id: str, **kwargs):
            return ledger.acknowledge_controller_action(decision_id, **kwargs)

        def complete_generation(self, decision_id: str, **kwargs):
            return ledger.complete_controller_generation(decision_id, **kwargs)

    class Adapter:
        def start_thread(self) -> ThreadIdentity:
            return ThreadIdentity("same-controller-thread")

        def run_turn(self, thread: ThreadIdentity, prompt: str, **kwargs):
            nonlocal run_calls
            run_calls += 1
            turn_id = "rate-limit-turn" if run_calls == 1 else "successful-turn"
            kwargs["turn_callback"](turn_id)
            if run_calls == 1:
                retry_at = (
                    (datetime.now(timezone.utc) + timedelta(minutes=1))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                raise TemporaryRateLimitAfterIdentity("temporary provider limit", retry_at=retry_at)
            status = ledger.controller_decision(decision.decision_id)
            bundle = ModelFacingControllerActionBundle(
                1,
                decision.decision_id,
                status.current_generation,
                "controller-rate-limit-recovered",
                status.revision,
                (),
                (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
                "same controller thread continued after the provider reset",
            )
            return TurnObservation(
                thread,
                turn_id,
                "completed",
                json.dumps(bundle.to_json()),
                bundle.to_json(),
                (),
            )

    result = ControllerGenerationRunner(
        Client(),  # type: ignore[arg-type]
        Adapter(),  # type: ignore[arg-type]
        decision_id=decision.decision_id,
        claimant_id="controller-generation",
        cwd=tmp_path,
        model="gpt-test",
        rate_limit_waiter=waited.append,
    ).run()

    assert result.outcome == "acknowledged"
    assert run_calls == 2
    assert len(waited) == 1
    generation = ledger.controller_generation(decision.decision_id, 1)
    assert generation.controller_thread_id == ThreadIdentity("same-controller-thread")
    assert generation.controller_turn_id == "successful-turn"
    assert generation.state is ControllerGenerationState.COMPLETED
    assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.ACKNOWLEDGED
    ledger.close()


def test_controller_prompt_receives_actionable_human_attention_context(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path)
    ledger.append_diagnostic(
        dispatch,
        kind="item/completed",
        text='{"status":"completed","summary":"bounded repair is ready"}',
    )
    ledger.record_retry_failure(
        dispatch,
        failure=RetryFailureClass.UNKNOWN,
        prior_thread_id="worker-thread",
    )
    ledger._db().execute(
        "UPDATE recovery_state SET worker_exit_classification = 'restart-observed-worker-dead', exit_code = 143 "
        "WHERE dispatch_id = ?",
        (dispatch,),
    )
    ledger._db().commit()
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")

    status = ledger.controller_decision(decision.decision_id)
    context = status.summary.context_json()
    assert context["dispatch_state"] == "human_attention_required"
    assert context["retry_policy_revision"] == 1
    assert context["retry_strategy"] == "human_attention_required"
    assert context["last_failure"] == "unknown"
    assert context["human_attention_reason"] == "retry budget exhausted for unknown"
    assert context["prior_thread_id"] == "worker-thread"
    assert context["recovery_state"] == "human_attention_required"
    assert context["worker_exit_classification"] == "restart-observed-worker-dead"
    assert context["worker_exit_code"] == 143
    activity = context["recent_activity"]
    assert isinstance(activity, list) and len(activity) == 1
    assert activity[0]["sequence"] == 1
    assert activity[0]["kind"] == "item/completed"
    assert activity[0]["text"] == '{"status":"completed","summary":"bounded repair is ready"}'

    runner = ControllerGenerationRunner.__new__(ControllerGenerationRunner)
    prompt = runner._prompt(status, revision=status.revision)
    assert '"dispatch_state":"human_attention_required"' in prompt
    assert '"retry_policy_revision":1' in prompt
    assert '"human_attention_reason":"retry budget exhausted for unknown"' in prompt
    assert '"prior_thread_id":"worker-thread"' in prompt
    assert '"worker_exit_classification":"restart-observed-worker-dead"' in prompt
    assert "automatic-retry prohibition does not prohibit a reasoned retry_dispatch" in prompt
    ledger.close()


def test_exhausted_source_notification_does_not_block_model_controller(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path)
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")
    delivery_id = f"wake/{dispatch}/checkpoint"

    for _ in range(2):
        wake = ledger.claim_wake(delivery_id)
        assert wake is not None and wake["state"] == "starting"
        ledger.record_wake_delivery(delivery_id, outcome="failed")

    status = ledger.controller_decision(decision.decision_id)
    assert status.state is ControllerDecisionState.AWAITING_CLAIM
    assert ledger.wake_outbox(state="failed")[0]["delivery_id"] == delivery_id
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.MODEL,
        claimant_id="controller-generation",
        expected_revision=status.revision,
    )
    assert claim.claimant_kind is ControllerClaimantKind.MODEL
    ledger.close()


def test_restart_releases_exact_legacy_notification_gated_decision(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    dispatch = _queue(ledger, tmp_path)
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")
    delivery_id = f"wake/{dispatch}/checkpoint"
    for _ in range(2):
        wake = ledger.claim_wake(delivery_id)
        assert wake is not None
        ledger.record_wake_delivery(delivery_id, outcome="failed")
    ledger._db().execute(
        "UPDATE controller_decisions SET state = 'human_attention_required', "
        "human_attention_reason = 'source wake delivery budget exhausted' WHERE decision_id = ?",
        (str(decision.decision_id),),
    )
    ledger._db().commit()

    assert ledger.reconcile_exhausted_wake_notifications() == 1
    repaired = ledger.controller_decision(decision.decision_id)
    assert repaired.state is ControllerDecisionState.AWAITING_CLAIM
    assert repaired.revision == 2
    assert ledger.reconcile_exhausted_wake_notifications() == 0
    ledger.close()


def test_explicit_refresh_rotates_only_pre_identity_controller_profile(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    ledger.prepare_controller_generation(decision.decision_id, generation=1)
    drifted = ledger.record_controller_profile_drift(decision.decision_id, generation=1)

    assert (
        ledger.authorize_controller_profile_refresh(
            native_profile_sha256="a" * 64,
            native_compatibility_sha256="b" * 64,
        )
        == 1
    )
    recovered = ledger.controller_decision(decision.decision_id)
    generation = ledger.controller_generation(decision.decision_id, 1)
    binding = ledger.queue_binding(recovered.dispatch_id)
    route = json.loads(str(ledger.queue_dispatch(recovered.dispatch_id)["route_json"]))
    assert recovered.state is ControllerDecisionState.AWAITING_CLAIM
    assert recovered.revision == drifted.revision + 1
    assert generation.state is ControllerGenerationState.PREPARED
    assert binding["native_profile_sha256"] == "a" * 64
    assert binding["native_compatibility_sha256"] == "b" * 64
    assert route["native_profile_sha256"] == "a" * 64
    assert route["native_compatibility_sha256"] == "b" * 64
    assert (
        ledger.authorize_controller_profile_refresh(
            native_profile_sha256="a" * 64,
            native_compatibility_sha256="b" * 64,
        )
        == 0
    )
    ledger.close()


def test_operator_authorizes_one_replacement_after_ambiguous_inspection(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    ledger.prepare_controller_generation(decision.decision_id, generation=1)
    ledger.bind_controller_generation_thread(
        decision.decision_id,
        generation=1,
        controller_thread_id="unreadable-controller-thread",
    )
    ledger.complete_controller_generation(
        decision.decision_id,
        generation=1,
        state=ControllerGenerationState.AMBIGUOUS,
    )
    inspection = ledger.reserve_controller_recovery_inspection(decision.decision_id)
    ledger.complete_controller_recovery_inspection(
        decision.decision_id,
        inspection_outcome=ControllerGenerationState.AMBIGUOUS.value,
        claim=inspection,
    )
    blocked = ledger.controller_decision(decision.decision_id)
    assert blocked.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED

    replacement = ledger.authorize_ambiguous_controller_replacement(
        decision.decision_id,
        expected_revision=blocked.revision,
        reason="operator verified the persisted controller task is unavailable",
    )
    recovered = ledger.controller_decision(decision.decision_id)
    assert int(replacement.generation) == 2
    assert replacement.state is ControllerGenerationState.PREPARED
    assert int(recovered.current_generation) == 2
    assert recovered.state is ControllerDecisionState.AWAITING_CLAIM
    with pytest.raises(StaleWriter, match="revision is stale"):
        ledger.authorize_ambiguous_controller_replacement(
            decision.decision_id,
            expected_revision=blocked.revision,
            reason="must not create generation three",
        )
    ledger.close()


def test_operator_replans_after_both_generations_fail_before_turn_identity(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)

    for generation_number in (1, 2):
        ledger.prepare_controller_generation(decision.decision_id, generation=generation_number)
        ledger.bind_controller_generation_thread(
            decision.decision_id,
            generation=generation_number,
            controller_thread_id=f"unreadable-controller-thread-{generation_number}",
        )
        ledger.complete_controller_generation(
            decision.decision_id,
            generation=generation_number,
            state=ControllerGenerationState.AMBIGUOUS,
        )
        inspection = ledger.reserve_controller_recovery_inspection(decision.decision_id)
        ledger.complete_controller_recovery_inspection(
            decision.decision_id,
            inspection_outcome=ControllerGenerationState.AMBIGUOUS.value,
            claim=inspection,
        )
        blocked = ledger.controller_decision(decision.decision_id)
        if generation_number == 1:
            ledger.authorize_ambiguous_controller_replacement(
                decision.decision_id,
                expected_revision=blocked.revision,
                reason="operator verified generation one had no turn",
            )

    blocked = ledger.controller_decision(decision.decision_id)
    replanned = ledger.authorize_controller_replan_after_local_pre_turn_failure(
        decision.decision_id,
        expected_revision=blocked.revision,
        reason="local schema validation was repaired before any provider turn existed",
    )
    assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.SUPERSEDED
    assert replanned.state is ControllerDecisionState.AWAITING_CLAIM
    assert replanned.summary.source_thread_id == "source-thread"
    assert "/checkpoint/2" in str(replanned.decision_id)
    assert ledger.wake_outbox(state="suppressed")[0]["decision_id"] == str(replanned.decision_id)
    assert ledger.queue_binding(replanned.dispatch_id)["checkpoint_armed"] == 0
    ledger.close()


def _delivered_decision(ledger: Ledger, root: Path):
    dispatch = _queue(ledger, root)
    decision = ledger.create_controller_decision(dispatch, kind="checkpoint", source_thread_id="source-thread")
    wake = ledger.claim_wake(f"wake/{dispatch}/checkpoint")
    assert wake is not None
    ledger.record_wake_delivery(str(wake["delivery_id"]), outcome="delivered", source_turn_id="wake-turn")
    return decision


def _prepare_failed_generation(ledger: Ledger, decision, *, claim_now: str, terminal_now: str) -> object:
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.MODEL,
        claimant_id="source-model",
        expected_revision=ledger.controller_decision(decision.decision_id).revision,
        lease_seconds=1,
        now=claim_now,
    )
    ledger.prepare_controller_generation(decision.decision_id, generation=1)
    ledger.bind_controller_generation_thread(
        decision.decision_id, generation=1, controller_thread_id="controller-thread"
    )
    ledger.bind_controller_generation_turn(
        decision.decision_id,
        generation=1,
        controller_thread_id="controller-thread",
        controller_turn_id="controller-turn",
    )
    ledger.complete_controller_generation(
        decision.decision_id,
        generation=1,
        state=ControllerGenerationState.FAILED,
        now=terminal_now,
    )
    return claim


def test_completed_recovery_commits_and_acknowledges_without_source_token(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    claim = _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "recovered-action",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "recovered exact completed bundle",
    )
    inspected = ledger.complete_controller_recovery_inspection(
        decision.decision_id,
        inspection_outcome="completed",
        claim=inspection_claim,
        bundle=bundle,
        now="2025-01-01T00:00:02Z",
    )
    assert inspected.state is ControllerGenerationState.COMPLETED

    receipt = ledger.submit_recovered_controller_actions(bundle)
    assert receipt.state == "committed"
    assert ledger.submit_recovered_controller_actions(bundle) == receipt
    acknowledged = ledger.acknowledge_recovered_controller_action(
        decision.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
    )
    assert acknowledged.state == "acknowledged"
    assert (
        ledger.acknowledge_recovered_controller_action(
            decision.decision_id,
            action_id=receipt.action_id,
            bundle_sha256=bundle.sha256,
            committed_revision=receipt.expected_revision + 1,
        )
        == acknowledged
    )
    row = (
        ledger._db()
        .execute(
            "SELECT claim_token_sha256 FROM controller_decisions WHERE decision_id = ?", (str(decision.decision_id),)
        )
        .fetchone()
    )
    assert row is not None and row["claim_token_sha256"] == hashlib.sha256(str(claim.token).encode()).hexdigest()
    ledger.close()


@pytest.mark.parametrize(
    ("action", "terminal_state"),
    (
        (
            ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),
            ControllerDecisionState.ACKNOWLEDGED,
        ),
        (
            ModelFacingControllerAction(
                ControllerActionKind.REQUIRE_HUMAN_ATTENTION,
                reason="persisted inspected action requires operator",
            ),
            ControllerDecisionState.HUMAN_ATTENTION_REQUIRED,
        ),
    ),
)
def test_completed_recovery_survives_expired_model_claim_and_restart(
    tmp_path: Path,
    action: ModelFacingControllerAction,
    terminal_state: ControllerDecisionState,
) -> None:
    """A completed one-read receipt remains recoverable after lease reap."""

    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    decision = _delivered_decision(ledger, tmp_path)
    claim = _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "recovered-after-reap",
        claim.revision,
        (),
        (action,),
        "persisted before claim lease expiry",
    )
    ledger.complete_controller_recovery_inspection(
        decision.decision_id,
        inspection_outcome="completed",
        claim=inspection_claim,
        bundle=bundle,
        now="2025-01-01T00:00:02Z",
    )
    with pytest.raises(StaleWriter, match="already completed"):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.MODEL,
            claimant_id="source-model",
            expected_revision=claim.revision,
            token=str(claim.token),
            now="2025-01-01T00:00:02Z",
        )

    released = ledger.reap_controller_claim(decision.decision_id, now="2025-01-01T00:00:03Z")
    assert released.state is ControllerDecisionState.AWAITING_CLAIM
    assert released.revision == claim.revision
    audit = (
        ledger._db()
        .execute(
            "SELECT claimant_kind, claimant_id, claim_token_sha256, claim_started_at, claim_lease_expires_at "
            "FROM controller_decisions WHERE decision_id = ?",
            (str(decision.decision_id),),
        )
        .fetchone()
    )
    assert audit is not None
    assert dict(audit) == {
        "claimant_kind": ControllerClaimantKind.MODEL.value,
        "claimant_id": "source-model",
        "claim_token_sha256": hashlib.sha256(str(claim.token).encode()).hexdigest(),
        "claim_started_at": "2025-01-01T00:00:00Z",
        "claim_lease_expires_at": None,
    }
    with pytest.raises(StaleWriter, match="already completed"):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.MODEL,
            claimant_id="source-model",
            expected_revision=released.revision,
            token=str(claim.token),
            now="2025-01-01T00:00:03Z",
        )
    with pytest.raises(StaleWriter, match="already completed"):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=released.revision,
            now="2025-01-01T00:00:03Z",
        )
    ledger.close()
    # Reopen the durable authority to model a process restart.  Recovery
    # consumes the persisted inspection bundle directly and performs no SDK
    # inspection or source-token replay.
    ledger = Ledger(path)
    receipt = ledger.submit_recovered_controller_actions(
        decision.decision_id,
        now="2025-01-01T00:00:04Z",
    )
    assert receipt.action_id == bundle.action_id
    assert ledger.submit_recovered_controller_actions(decision.decision_id) == receipt
    acknowledged = ledger.acknowledge_recovered_controller_action(
        decision.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=receipt.bundle_sha256,
        committed_revision=receipt.expected_revision + 1,
        now="2025-01-01T00:00:05Z",
    )
    assert acknowledged.state == "acknowledged"
    assert (
        ledger.acknowledge_recovered_controller_action(
            decision.decision_id,
            action_id=receipt.action_id,
            bundle_sha256=receipt.bundle_sha256,
            committed_revision=receipt.expected_revision + 1,
        )
        == acknowledged
    )
    terminal = ledger.controller_decision(decision.decision_id)
    assert terminal.state is terminal_state
    committed_audit = (
        ledger._db()
        .execute(
            "SELECT d.claimant_kind, d.claimant_id, d.claim_token_sha256, d.claim_lease_expires_at, "
            "a.claimant_kind AS outbox_kind, a.claimant_id AS outbox_id "
            "FROM controller_decisions d JOIN controller_action_outbox a ON a.decision_id = d.decision_id "
            "WHERE d.decision_id = ?",
            (str(decision.decision_id),),
        )
        .fetchone()
    )
    assert committed_audit is not None
    assert dict(committed_audit) == {
        "claimant_kind": ControllerClaimantKind.MODEL.value,
        "claimant_id": "source-model",
        "claim_token_sha256": hashlib.sha256(str(claim.token).encode()).hexdigest(),
        "claim_lease_expires_at": None,
        "outbox_kind": ControllerClaimantKind.MODEL.value,
        "outbox_id": "source-model",
    }
    ledger.close()

    reopened = Ledger(path)
    assert reopened.controller_decision(decision.decision_id).state is terminal_state
    assert reopened.controller_action_pending_acknowledgement(decision.decision_id) is False
    reopened_audit = (
        reopened._db()
        .execute(
            "SELECT d.claimant_kind, d.claimant_id, a.claimant_kind AS outbox_kind, "
            "a.claimant_id AS outbox_id, a.state AS outbox_state "
            "FROM controller_decisions d JOIN controller_action_outbox a ON a.decision_id = d.decision_id "
            "WHERE d.decision_id = ?",
            (str(decision.decision_id),),
        )
        .fetchone()
    )
    assert reopened_audit is not None
    assert dict(reopened_audit) == {
        "claimant_kind": ControllerClaimantKind.MODEL.value,
        "claimant_id": "source-model",
        "outbox_kind": ControllerClaimantKind.MODEL.value,
        "outbox_id": "source-model",
        "outbox_state": "acknowledged",
    }
    reopened.close()


def test_completed_inspection_claimant_audit_rejects_substitution_after_reap(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    decision = _delivered_decision(ledger, tmp_path)
    claim = _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(
        decision.decision_id,
        now="2025-01-01T00:00:02Z",
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "claimant-audit-substitution",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "claimant audit is immutable",
    )
    ledger.complete_controller_recovery_inspection(
        decision.decision_id,
        inspection_outcome="completed",
        claim=inspection_claim,
        bundle=bundle,
        now="2025-01-01T00:00:02Z",
    )
    ledger.reap_controller_claim(decision.decision_id, now="2025-01-01T00:00:03Z")
    ledger._db().execute(
        "UPDATE controller_decisions SET claimant_id = 'substituted-model' WHERE decision_id = ?",
        (str(decision.decision_id),),
    )
    ledger._db().commit()
    ledger.close()

    with pytest.raises(CorruptSchemaError, match="claimant audit"):
        Ledger(path)


def test_failed_inspection_rolls_one_generation_only_after_claim_expiry(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    rolled = ledger.complete_controller_recovery_inspection(
        decision.decision_id, inspection_outcome="failed", claim=inspection_claim, now="2025-01-01T00:00:02Z"
    )
    assert rolled.generation == 2
    assert rolled.predecessor_generation == 1
    assert rolled.source_kind == "replacement_controller"
    assert ledger.controller_decision(decision.decision_id).state.value == "awaiting_claim"
    ledger.close()


def test_failed_inspection_with_live_claim_fails_closed_without_replacement(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:00.500000Z",
    )
    inspection_claim = ledger.reserve_controller_recovery_inspection(
        decision.decision_id, now="2025-01-01T00:00:00.500000Z"
    )
    inspected = ledger.complete_controller_recovery_inspection(
        decision.decision_id, inspection_outcome="failed", claim=inspection_claim, now="2025-01-01T00:00:00.500000Z"
    )
    assert inspected.generation == 1
    assert ledger.controller_decision(decision.decision_id).state.value == "human_attention_required"
    with pytest.raises(RecordNotFound):
        ledger.controller_generation(decision.decision_id, 2)
    ledger.close()


def test_human_claim_suppresses_wake_and_expired_token_cannot_replay(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = ledger.create_controller_decision(
        _queue(ledger, tmp_path), kind="checkpoint", source_thread_id="source-thread"
    )
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=decision.revision,
        lease_seconds=1,
        now="2025-01-01T00:00:00Z",
    )
    wake = ledger.wake_outbox()[0]
    assert wake["state"] == "suppressed"
    released = ledger.reap_controller_claim(decision.decision_id, now="2025-01-01T00:00:02Z")
    assert released.state.value == "awaiting_claim"
    with pytest.raises(StaleWriter):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=released.revision,
            token=str(claim.token),
            now="2025-01-01T00:00:02Z",
        )
    assert ledger.wake_outbox()[0]["state"] == "suppressed"
    ledger.close()


def test_controller_claim_is_expired_at_exact_lease_boundary(tmp_path: Path) -> None:
    """A lease is no longer live when the current instant equals its expiry."""

    ledger = Ledger(tmp_path / "workflow.db")
    decision = ledger.create_controller_decision(
        _queue(ledger, tmp_path), kind="checkpoint", source_thread_id="source-thread"
    )
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=decision.revision,
        lease_seconds=1,
        now="2025-01-01T00:00:00Z",
    )

    with pytest.raises(StaleWriter, match="expired"):
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=claim.revision,
            token=str(claim.token),
            now=claim.lease_expires_at,
        )
    released = ledger.reap_controller_claim(decision.decision_id, now=claim.lease_expires_at)
    assert released.state is ControllerDecisionState.AWAITING_CLAIM
    ledger.close()


def test_recovery_runner_uses_one_read_and_recovered_typed_operations(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )

    class Client:
        def status(self, decision_id: str):
            return ledger.controller_decision(decision_id)

        def generation(self, decision_id: str, generation: int | None = None):
            return ledger.controller_generation(decision_id, generation)

        def reserve_recovery_inspection(self, decision_id: str):
            return ledger.reserve_controller_recovery_inspection(decision_id, now="2025-01-01T00:00:02Z")

        def complete_recovery_inspection(self, decision_id: str, *, inspection_outcome: str, claim, bundle=None):
            return ledger.complete_controller_recovery_inspection(
                decision_id,
                inspection_outcome=inspection_outcome,
                claim=claim,
                bundle=bundle,
                now="2025-01-01T00:00:02Z",
            )

        def submit_recovered_actions(self, decision_id):
            return ledger.submit_recovered_controller_actions(decision_id, now="2025-01-01T00:00:02Z")

        def acknowledge_recovered(
            self, decision_id: str, *, action_id: str, bundle_sha256: str, committed_revision: int
        ):
            return ledger.acknowledge_recovered_controller_action(
                decision_id,
                action_id=action_id,
                bundle_sha256=bundle_sha256,
                committed_revision=committed_revision,
                now="2025-01-01T00:00:02Z",
            )

    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        1,
        "runner-recovered-action",
        2,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "runner recovery",
    )

    class Adapter:
        reads = 0

        def inspect_controller_thread(self, thread, *, decision_id, generation):
            self.reads += 1
            return ControllerThreadInspection(
                thread.id,
                int(generation),
                ControllerThreadInspectionKind.COMPLETED,
                turn_id="controller-turn",
                bundle=bundle,
            )

    adapter = Adapter()
    result = ControllerGenerationRecovery(Client(), adapter).recover(decision.decision_id)
    assert result.outcome == "acknowledged"
    assert result.action_id == bundle.action_id
    assert adapter.reads == 1
    assert ledger.controller_decision(decision.decision_id).state.value == "acknowledged"
    ledger.close()


def test_recovered_actions_require_exact_persisted_bundle(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    claim = _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    inspection = ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    persisted = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "persisted-action",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "persisted",
    )
    ledger.complete_controller_recovery_inspection(
        decision.decision_id,
        inspection_outcome="completed",
        claim=inspection,
        bundle=persisted,
        now="2025-01-01T00:00:02Z",
    )
    invented = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "invented-action",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.REARM_CHECKPOINT, checkpoint_seconds=1),),
        "invented",
    )
    with pytest.raises(StaleWriter):
        ledger.submit_controller_actions(
            invented,
            claimant_id="recovery-inspection",
            token="",
            allow_recovered=True,
            now="2025-01-01T00:00:03Z",
        )
    assert ledger.controller_decision(decision.decision_id).action_id is None
    ledger.close()


def test_human_attention_action_stays_attention_after_ack(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    status = ledger.controller_decision(decision.decision_id)
    claim = ledger.claim_controller_decision(
        status.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=status.revision,
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        status.decision_id,
        claim.generation,
        "attention-action",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.REQUIRE_HUMAN_ATTENTION, reason="needs operator"),),
        "attention",
    )
    receipt = ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
    acknowledged = ledger.acknowledge_controller_action(
        status.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
        claimant_id="operator",
        token=str(claim.token),
    )
    assert acknowledged.state == "acknowledged"
    ledger.close()
    reopened = Ledger(tmp_path / "workflow.db")
    assert reopened.controller_decision(status.decision_id).state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
    reopened.close()


@pytest.mark.parametrize("mutation", ["actions", "generation", "extra"])
def test_effect_receipt_is_cross_bound_to_the_committed_bundle(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    decision = _delivered_decision(ledger, tmp_path)
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=ledger.controller_decision(decision.decision_id).revision,
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "receipt-bound-action",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "receipt binding",
    )
    ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
    row = (
        ledger._db()
        .execute(
            "SELECT effect_receipt_json FROM controller_action_outbox WHERE action_id = ?",
            (bundle.action_id,),
        )
        .fetchone()
    )
    assert row is not None
    receipt = json.loads(str(row["effect_receipt_json"]))
    if mutation == "actions":
        receipt["applied_actions"] = [ControllerActionKind.REARM_CHECKPOINT.value]
    elif mutation == "generation":
        receipt["generation"] = 2
    else:
        receipt["invented_fact"] = "accepted"
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ledger._db().execute(
        "UPDATE controller_action_outbox SET effect_receipt_json = ?, effect_receipt_sha256 = ? WHERE action_id = ?",
        (encoded, hashlib.sha256(encoded.encode()).hexdigest(), bundle.action_id),
    )
    ledger._db().commit()
    ledger.close()

    with pytest.raises(CorruptSchemaError):
        Ledger(path)


def test_human_can_claim_and_resolve_an_ambiguous_attention_decision(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    decision = _delivered_decision(ledger, tmp_path)
    _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    with pytest.raises(StaleWriter):
        ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:05:03Z")
    attention = ledger.controller_decision(decision.decision_id)
    assert attention.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED

    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=attention.revision,
        now="2025-01-01T00:05:04Z",
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "resolve-ambiguous-attention",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
        "operator resolved ambiguous recovery",
    )
    receipt = ledger.submit_controller_actions(
        bundle,
        claimant_id="operator",
        token=str(claim.token),
        now="2025-01-01T00:05:05Z",
    )
    ledger.acknowledge_controller_action(
        decision.decision_id,
        action_id=receipt.action_id,
        bundle_sha256=bundle.sha256,
        committed_revision=receipt.expected_revision + 1,
        claimant_id="operator",
        token=str(claim.token),
        now="2025-01-01T00:05:06Z",
    )
    ledger.close()
    reopened = Ledger(path)
    assert reopened.controller_decision(decision.decision_id).state is ControllerDecisionState.ACKNOWLEDGED
    reopened.close()


def test_human_attention_commit_is_acknowledged_after_restart_without_sdk_read(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = Ledger(path)
    decision = _delivered_decision(ledger, tmp_path)
    claim = ledger.claim_controller_decision(
        decision.decision_id,
        claimant_kind=ControllerClaimantKind.HUMAN,
        claimant_id="operator",
        expected_revision=ledger.controller_decision(decision.decision_id).revision,
    )
    bundle = ModelFacingControllerActionBundle(
        1,
        decision.decision_id,
        claim.generation,
        "attention-before-restart",
        claim.revision,
        (),
        (ModelFacingControllerAction(ControllerActionKind.REQUIRE_HUMAN_ATTENTION, reason="operator follow-up"),),
        "attention survives restart",
    )
    ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
    ledger.close()
    ledger = Ledger(path)

    class Client:
        def status(self, decision_id: str):
            return ledger.controller_decision(decision_id)

        def generation(self, decision_id: str, generation: int | None = None):
            return ledger.controller_generation(decision_id, generation)

        def acknowledge_recovered(
            self, decision_id: str, *, action_id: str, bundle_sha256: str, committed_revision: int
        ):
            return ledger.acknowledge_recovered_controller_action(
                decision_id,
                action_id=action_id,
                bundle_sha256=bundle_sha256,
                committed_revision=committed_revision,
            )

    class NoInspectionAdapter:
        def inspect_controller_thread(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("a committed human action must not consume an SDK inspection")

    result = ControllerGenerationRecovery(Client(), NoInspectionAdapter()).recover(decision.decision_id)
    assert result.outcome == "acknowledged"
    assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
    ledger._db().execute(
        "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0 WHERE dispatch_id = ?",
        (str(decision.dispatch_id),),
    )
    ledger._db().commit()
    rearmed = ledger.rearm_checkpoint(decision.dispatch_id, seconds=60)
    assert rearmed["checkpoint_armed"] == 1
    outbox = (
        ledger._db()
        .execute("SELECT state, acknowledged_at FROM controller_action_outbox WHERE action_id = ?", (bundle.action_id,))
        .fetchone()
    )
    assert outbox is not None and outbox["state"] == "acknowledged" and outbox["acknowledged_at"] is not None
    ledger.close()


def test_expired_inspection_is_durable_human_attention(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    decision = _delivered_decision(ledger, tmp_path)
    _prepare_failed_generation(
        ledger,
        decision,
        claim_now="2025-01-01T00:00:00Z",
        terminal_now="2025-01-01T00:00:02Z",
    )
    ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:00:02Z")
    with pytest.raises(StaleWriter):
        ledger.reserve_controller_recovery_inspection(decision.decision_id, now="2025-01-01T00:05:03Z")
    assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
    assert ledger.controller_generation(decision.decision_id).inspection_outcome == "ambiguous"
    ledger.close()
