from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest

from codex_flow.backends.codex_sdk import (
    CodexSdkAdapter,
    CodexSdkConfig,
    ThreadInspectionKind,
    _terminal_from_thread_snapshot,
)
from codex_flow.contracts import ModelFacingControllerActionBundle, model_facing_result_schema_sha256
from codex_flow.control_client import (
    ControlClientError,
    ControlCommandPostSendUncertain,
    ControlCommandRejected,
    LiveWorkerControlClient,
    _decode_action,
    _decode_command,
    _response,
)
from codex_flow.domain import (
    ControlAcknowledgement,
    ControlCommand,
    ControlCommandKind,
    ControlCommandState,
    ControllerActionKind,
    ControllerActionReceipt,
    ControllerClaimantKind,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerDecisionStatus,
    ControllerDecisionSummary,
    ConversationContent,
    ConversationContentKind,
    ConversationHistoryPage,
    ConversationHistoryStatus,
    ConversationMessageFragment,
    ConversationSpeaker,
    ConversationSubjectKind,
    ConversationTurnSlice,
    DiagnosticEvent,
    DispatchId,
    Generation,
    LiveWorkerStatus,
    ReasoningEffort,
    RecoveryStrategy,
    RetryFailureClass,
    RetryPolicyFacts,
    Sandbox,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
)
from codex_flow.ipc import IpcError, encode_frame
from codex_flow.ledger import Ledger, StaleWriter
from codex_flow.supervisor import Supervisor
from codex_flow.tui import CodexFlowTerminalApp, ConfirmActionScreen, SteerActionScreen
from codex_flow.tui_client import TerminalUiClient, TerminalUiOfflineError
from codex_flow.tui_models import WorkerView

DISPATCH = "run/milestone/executor/1"


def _tui_worker() -> LiveWorkerStatus:
    return LiveWorkerStatus(
        DispatchId(DISPATCH),
        "running",
        1,
        2,
        ThreadIdentity("thread-1"),
        "turn-1",
        RetryPolicyFacts(),
        (DiagnosticEvent(1, "agent_message", "2026-08-30T10:00:00Z", "working"),),
    )


def _tui_decision() -> ControllerDecisionStatus:
    summary = ControllerDecisionSummary(
        DispatchId(DISPATCH),
        "checkpoint",
        ControllerDecisionState.AWAITING_CLAIM,
        "Review the bounded worker state.",
        "thread-1",
        "2026-08-30T11:00:00Z",
        3,
        (),
    )
    return ControllerDecisionStatus(
        ControllerDecisionId(f"decision/{DISPATCH}/checkpoint/1"),
        DispatchId(DISPATCH),
        "checkpoint",
        ControllerDecisionState.AWAITING_CLAIM,
        3,
        Generation(1),
        2,
        1,
        None,
        None,
        None,
        None,
        None,
        "2026-08-30T11:00:00Z",
        summary,
    )


def test_thread_identity_serializes_as_the_raw_sdk_id() -> None:
    command = _TerminalUiLiveClient._command(
        DISPATCH,
        kind=ControlCommandKind.INTERRUPT,
        payload=None,
        command_id="raw-thread-command",
        generation=1,
        attempt=1,
        thread_id="thread-raw",
        turn_id="turn-raw",
    )
    acknowledgement = ControlAcknowledgement(
        "raw-thread-command",
        DispatchId(DISPATCH),
        Generation(1),
        1,
        ThreadIdentity("thread-raw"),
        "turn-raw",
        ControlCommandKind.INTERRUPT,
        ControlCommandState.ACKNOWLEDGED,
    )

    assert command.to_json()["thread_id"] == "thread-raw"
    assert acknowledgement.to_json()["thread_id"] == "thread-raw"
    assert _tui_worker().to_json()["thread_id"] == "thread-1"
    assert "ThreadIdentity(" not in json.dumps(
        (command.to_json(), acknowledgement.to_json(), _tui_worker().to_json()), sort_keys=True
    )


def _ledger(root: Path) -> Ledger:
    ledger = Ledger(root / "workflow.db")
    ledger.create_run("run")
    ledger.create_milestone("run", "milestone")
    ledger.claim_dispatch("run", "milestone", "executor", 1)
    ledger.enqueue_dispatch(
        DISPATCH,
        backend="sdk_headless",
        capsule_json='{"model":"test","prompt":"bounded"}',
        route_json='{"leaf_worker_policy":{"agents.enabled":false,"features.multi_agent":false,"config_overrides":[]}}',
        workspace_path=root,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    return ledger


def _active_command_ledger(root: Path) -> Ledger:
    ledger = _ledger(root)
    authority = ledger.acquire_supervisor(
        repository_root=root,
        state_root=root,
        pid=1,
        process_birth_identity="test",
        executable_digest="a" * 64,
        version="test",
        owner_nonce_sha256="b" * 64,
        lease_seconds=60,
    )
    epoch = int(authority["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'thread-1' WHERE dispatch_id = ?", (DISPATCH,))
    ledger._db().commit()
    return ledger


def test_diagnostic_ring_is_bounded_and_secretless() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _ledger(root)
        for index in range(129):
            ledger.append_diagnostic(
                DISPATCH,
                kind="agent_message",
                text=f"event-{index} api_key=secret-{index}",
            )
        rows = ledger.recent_activity(DISPATCH)
        assert len(rows) == 128
        assert rows[0]["text"] == "event-1 api_key=[REDACTED]"
        assert "secret-" not in json.dumps(rows)
        ledger.close()


def test_diagnostic_ring_evicts_by_bytes_without_touching_queue_authority() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _ledger(root)
        text = "x" * 8192
        for _ in range(9):
            ledger.append_diagnostic(DISPATCH, kind="chunk", text=text)
        rows = ledger.recent_activity(DISPATCH)
        assert len(rows) == 8
        assert ledger.queue_dispatch(DISPATCH)["state"] == "queued"
        ledger.close()


def test_diagnostic_ring_counts_digest_source_payload_bytes() -> None:
    with TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        for _ in range(9):
            ledger.append_diagnostic(DISPATCH, kind="payload", payload=b"x" * 8192)
        assert len(ledger.recent_activity(DISPATCH)) == 8
        ledger.close()


def test_diagnostic_sequence_replay_is_idempotent_and_conflicts_fail_closed() -> None:
    with TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        first = ledger.append_diagnostic(
            DISPATCH,
            kind="agent_message",
            text="same event",
            sequence=1,
            occurred_at="2026-08-27T00:00:00Z",
        )
        replay = ledger.append_diagnostic(
            DISPATCH,
            kind="agent_message",
            text="same event",
            sequence=1,
            occurred_at="2026-08-27T00:00:01Z",
        )
        assert replay == first
        with pytest.raises(StaleWriter, match="replay conflicts"):
            ledger.append_diagnostic(DISPATCH, kind="agent_message", text="different event", sequence=1)
        ledger.close()


def test_diagnostic_storm_avoids_whole_ledger_validation_and_rolls_back_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        whole_ledger_validations = 0
        original = ledger._validate_rows

        def counted_validation() -> None:
            nonlocal whole_ledger_validations
            whole_ledger_validations += 1
            original()

        monkeypatch.setattr(ledger, "_validate_rows", counted_validation)
        for sequence in range(1, 2_001):
            ledger.append_diagnostic(
                DISPATCH,
                kind="item/commandExecution/outputDelta",
                text="x" * 64,
                sequence=sequence,
            )
        assert whole_ledger_validations == 0
        assert len(ledger.recent_activity(DISPATCH)) == 128

        before = ledger.recent_activity(DISPATCH)
        with pytest.raises(StaleWriter, match="replay conflicts"):
            ledger.append_diagnostic(
                DISPATCH,
                kind="item/commandExecution/outputDelta",
                text="conflict",
                sequence=2_000,
            )
        assert ledger.recent_activity(DISPATCH) == before
        ledger.append_diagnostic(DISPATCH, kind="turn/completed", text="completed", sequence=2_001)
        ledger.close()


def test_retry_budgets_and_backoff_gate_are_durable() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _ledger(root)
        authority = ledger.acquire_supervisor(
            repository_root=root,
            state_root=root,
            pid=1,
            process_birth_identity="test",
            executable_digest="a" * 64,
            version="test",
            owner_nonce_sha256="b" * 64,
            lease_seconds=60,
        )
        epoch = int(authority["epoch"])
        first = ledger.record_retry_failure(DISPATCH, failure=RetryFailureClass.PRE_IDENTITY_TRANSPORT)
        assert first["pre_identity_used"] == 1
        assert first["strategy"] == RecoveryStrategy.BACKOFF.value
        assert ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64) is None
        for _ in range(4):
            ledger.record_retry_failure(
                DISPATCH,
                failure=RetryFailureClass.PRE_IDENTITY_TRANSPORT,
                next_eligible_at="2000-01-01T00:00:00Z",
            )
        exhausted = ledger.record_retry_failure(
            DISPATCH,
            failure=RetryFailureClass.PRE_IDENTITY_TRANSPORT,
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        assert exhausted["strategy"] == RecoveryStrategy.HUMAN_ATTENTION.value
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        ledger.close()


def test_control_commands_redact_payload_and_require_sent_ack() -> None:
    with TemporaryDirectory() as directory:
        ledger = _active_command_ledger(Path(directory))
        command = ledger.create_control_command(
            DISPATCH,
            command_id="command-1",
            generation=1,
            attempt=1,
            thread_id="thread-1",
            turn_id="turn-1",
            kind="steer",
            payload="continue api_key=secret",
        )
        assert command["payload"] == "continue api_key=[REDACTED]"
        with pytest.raises(StaleWriter, match="sent command"):
            ledger.acknowledge_control_command("command-1", state=ControlCommandState.ACKNOWLEDGED)
        ledger.claim_control_commands(
            DISPATCH,
            generation=1,
            attempt=1,
            thread_id="thread-1",
            turn_id="turn-1",
        )
        acknowledged = ledger.acknowledge_control_command(
            "command-1",
            state=ControlCommandState.ACKNOWLEDGED,
            acknowledgement={"detail": "token=secret"},
        )
        assert "secret" not in str(acknowledged["acknowledgement_json"])
        replay = ledger.acknowledge_control_command(
            "command-1",
            state=ControlCommandState.ACKNOWLEDGED,
            acknowledgement={"detail": "token=[REDACTED]"},
        )
        assert replay == acknowledged
        with pytest.raises(ValueError, match="unsupported shape"):
            ledger.acknowledge_control_command(
                "command-1",
                state=ControlCommandState.ACKNOWLEDGED,
                acknowledgement={"detail": "applied", "extra": "discarded"},
            )
        ledger.close()


def test_public_control_envelopes_require_exact_response_digest_and_acknowledgement_shapes() -> None:
    payload = "continue"
    command = {
        "command_id": "command-1",
        "dispatch_id": DISPATCH,
        "submission_sequence": 1,
        "generation": 1,
        "attempt": 1,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "kind": "steer",
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "state": "acknowledged",
        "acknowledgement_json": json.dumps(
            {"detail": "applied", "terminal_status": "external_blocked"}, separators=(",", ":"), sort_keys=True
        ),
        "created_at": "2026-08-27T00:00:00Z",
        "sent_at": "2026-08-27T00:00:01Z",
        "acknowledged_at": "2026-08-27T00:00:02Z",
    }
    decoded = _decode_command(command)
    assert decoded.payload_sha256 == command["payload_sha256"]
    assert decoded.acknowledgement == "applied"
    assert decoded.acknowledgement_terminal_status == "external_blocked"

    with pytest.raises(ControlClientError, match="control request rejected"):
        _response({"version": 1, "ok": True, "command": command, "extra": True}, operation="steer")
    with pytest.raises(ControlClientError, match="payload digest"):
        _decode_command({**command, "payload_sha256": "A" * 64})
    with pytest.raises(ControlClientError, match="command acknowledgement"):
        _decode_command(
            {
                **command,
                "acknowledgement_json": json.dumps(
                    {"detail": "applied", "terminal_status": "completed", "extra": True}
                ),
            }
        )
    with pytest.raises(ControlClientError, match="before a terminal command state"):
        _decode_command({**command, "state": "sent"})
    with pytest.raises(ControlClientError, match="without acknowledgement facts"):
        _decode_command({**command, "acknowledgement_json": None})


def test_live_control_client_distinguishes_rejection_from_post_send_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = LiveWorkerControlClient(tmp_path / "supervisor.sock")
    facts = {
        "generation": 1,
        "attempt": 1,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "command_id": "retained-id",
    }
    monkeypatch.setattr(
        "codex_flow.control_client.send_request",
        lambda *_args, **_kwargs: {"version": 1, "ok": False, "error": "request_rejected"},
    )
    with pytest.raises(ControlCommandRejected, match="explicitly rejected"):
        client.interrupt(DISPATCH, **facts)

    monkeypatch.setattr(
        "codex_flow.control_client.send_request",
        lambda *_args, **_kwargs: {"version": 1, "ok": True, "command": None},
    )
    with pytest.raises(ControlCommandPostSendUncertain, match="malformed"):
        client.interrupt(DISPATCH, **facts)

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise IpcError("reply lost")

    monkeypatch.setattr("codex_flow.control_client.send_request", unavailable)
    with pytest.raises(ControlCommandPostSendUncertain, match="uncertain"):
        client.interrupt(DISPATCH, **facts)


def test_live_control_client_emits_and_decodes_closed_compatibility_rebind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def exact_response(_socket: Path, request: dict[str, object], **_kwargs: object) -> dict[str, object]:
        captured.update(request)
        rebind_json = json.dumps(request["compatibility_rebind"], sort_keys=True, separators=(",", ":"))
        return {
            "version": 1,
            "ok": True,
            "action": {
                "action_id": request["action_id"],
                "dispatch_id": request["dispatch_id"],
                "expected_revision": request["expected_revision"],
                "action_kind": request["action_kind"],
                "reason": request["reason"],
                "requested_budget_json": None,
                "compatibility_rebind_json": rebind_json,
                "applied_revision": 8,
                "created_at": "2026-09-01T00:00:00Z",
            },
        }

    monkeypatch.setattr("codex_flow.control_client.send_request", exact_response)
    action = LiveWorkerControlClient(tmp_path / "supervisor.sock").rebind_compatibility(
        DISPATCH,
        action_id="authorized-runtime-rebind",
        expected_revision=7,
        reason="human authorized exact installed runtime",
        expected_compatibility_sha256="a" * 64,
        proposed_compatibility_sha256="b" * 64,
        expected_profile_sha256="c" * 64,
        proposed_profile_sha256="d" * 64,
        expected_generation=1,
        expected_attempt=10,
    )

    assert captured == {
        "version": 1,
        "operation": "recovery_action",
        "dispatch_id": DISPATCH,
        "action_id": "authorized-runtime-rebind",
        "expected_revision": 7,
        "action_kind": "compatibility_rebind",
        "reason": "human authorized exact installed runtime",
        "compatibility_rebind": {
            "expected_compatibility_sha256": "a" * 64,
            "proposed_compatibility_sha256": "b" * 64,
            "expected_profile_sha256": "c" * 64,
            "proposed_profile_sha256": "d" * 64,
            "expected_generation": 1,
            "expected_attempt": 10,
        },
    }
    assert action.compatibility_rebind is not None
    assert action.compatibility_rebind.proposed_compatibility_sha256 == "b" * 64
    assert action.applied_revision == 8


def test_live_control_client_decodes_legacy_compatibility_receipt_without_inventing_profile_facts() -> None:
    legacy = {
        "expected_compatibility_sha256": "a" * 64,
        "proposed_compatibility_sha256": "b" * 64,
        "expected_generation": 1,
        "expected_attempt": 10,
    }
    action = _decode_action(
        {
            "action_id": "historical-runtime-rebind",
            "dispatch_id": DISPATCH,
            "expected_revision": 24,
            "action_kind": "compatibility_rebind",
            "reason": "historical compatibility-only authorization",
            "requested_budget_json": None,
            "compatibility_rebind_json": json.dumps(legacy, sort_keys=True, separators=(",", ":")),
            "applied_revision": 25,
            "created_at": "2026-09-01T00:00:00Z",
        }
    )
    assert action.compatibility_rebind is not None
    assert action.compatibility_rebind.expected_profile_sha256 is None
    assert action.compatibility_rebind.to_json() == legacy


def test_live_control_client_decodes_one_provider_transient_grant_receipt() -> None:
    action = _decode_action(
        {
            "action_id": "provider-one-step-grant",
            "dispatch_id": DISPATCH,
            "expected_revision": 3,
            "action_kind": "budget_change",
            "reason": "authorize one provider continuation",
            "requested_budget_json": json.dumps(
                {
                    "pre_identity_budget": 5,
                    "invalid_chain_budget": 1,
                    "schema_envelope_budget": 2,
                    "post_identity_loss_budget": 1,
                    "provider_transient_budget": 4,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "compatibility_rebind_json": None,
            "applied_revision": 4,
            "created_at": "2026-09-01T00:00:00Z",
        }
    )
    assert action.requested_budget is not None
    assert action.requested_budget.provider_transient_budget == 4
    assert action.to_json()["requested_budget"] == {
        "pre_identity_budget": 5,
        "invalid_chain_budget": 1,
        "schema_envelope_budget": 2,
        "post_identity_loss_budget": 1,
    }


class _DelayedTurn:
    def __init__(self) -> None:
        self.id = "turn-1"
        self.release = threading.Event()
        self.steer_text: str | None = None

    def steer(self, value: object) -> None:
        self.steer_text = str(value)
        self.release.set()

    def interrupt(self) -> None:
        self.release.set()

    def stream(self) -> list[Any]:
        self.release.wait(timeout=2)
        text = json.dumps({"response": self.steer_text or "original"})
        return [
            SimpleNamespace(
                method="item/completed", payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(text=text)))
            ),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(
                    turn=SimpleNamespace(id=self.id, status=SimpleNamespace(value="completed"), error=None)
                ),
            ),
        ]


class _DelayedThread:
    id = "thread-1"

    def __init__(self) -> None:
        self.turn_handle = _DelayedTurn()

    def turn(self, _input: object, **_kwargs: object) -> _DelayedTurn:
        return self.turn_handle


class _DelayedClient:
    def __init__(self) -> None:
        self.thread_handle = _DelayedThread()

    def thread_start(self, **_kwargs: object) -> _DelayedThread:
        return self.thread_handle

    def thread_resume(self, _thread_id: str, **_kwargs: object) -> _DelayedThread:
        return self.thread_handle

    def close(self) -> None:
        return None


def test_delayed_turn_steer_changes_same_live_turn_response() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        sdk = SimpleNamespace(
            Sandbox=SimpleNamespace(read_only="read-only"),
            ApprovalMode=SimpleNamespace(deny_all="deny-all"),
            ReasoningEffort=SimpleNamespace(medium="medium"),
            SkillInput=None,
            version="test",
        )
        client = _DelayedClient()
        adapter = CodexSdkAdapter(
            CodexSdkConfig("test", ReasoningEffort.MEDIUM, Sandbox.READ_ONLY, root),
            client_factory=lambda: client,
            sdk=sdk,
        )
        thread = adapter.start_thread()
        result: list[object] = []
        error: list[BaseException] = []

        def run() -> None:
            try:
                result.append(adapter.run_turn(thread, "initial"))
            except BaseException as exc:  # pragma: no cover - diagnostic assertion path
                error.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        while adapter.current_turn(thread) is None:
            worker.join(timeout=0.01)
        handle = adapter.current_turn(thread)
        assert handle is not None
        handle.steer("steer-now")
        worker.join(timeout=2)
        assert not error
        assert result and result[0].final_response == '{"response": "steer-now"}'
        adapter.close()


def test_turn_callback_failure_releases_live_turn_handle() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        sdk = SimpleNamespace(
            Sandbox=SimpleNamespace(read_only="read-only"),
            ApprovalMode=SimpleNamespace(deny_all="deny-all"),
            ReasoningEffort=SimpleNamespace(medium="medium"),
            SkillInput=None,
            version="test",
        )
        client = _DelayedClient()
        adapter = CodexSdkAdapter(
            CodexSdkConfig("test", ReasoningEffort.MEDIUM, Sandbox.READ_ONLY, root),
            client_factory=lambda: client,
            sdk=sdk,
        )
        thread = adapter.start_thread()
        with pytest.raises(TerminalFailureAfterIdentity, match="SDK turn failed"):
            adapter.run_turn(
                thread, "initial", turn_callback=lambda _turn: (_ for _ in ()).throw(RuntimeError("callback"))
            )
        assert adapter.current_turn(thread) is None
        adapter.close()


def test_interrupted_thread_snapshot_is_distinct_terminal_evidence() -> None:
    snapshot = {
        "thread": {
            "id": "thread-1",
            "turns": [{"id": "turn-1", "status": "interrupted", "items": []}],
        }
    }
    inspection = _terminal_from_thread_snapshot(snapshot, "thread-1")
    assert inspection.kind is ThreadInspectionKind.INTERRUPTED
    assert inspection.turn_id == "turn-1"


class _TerminalUiLiveClient:
    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.calls: list[tuple[str, object]] = []

    def status(self) -> tuple[LiveWorkerStatus, ...]:
        if not self.online:
            raise ControlClientError("supervisor control endpoint is unavailable")
        return (_tui_worker(),)

    @staticmethod
    def _command(dispatch_id: str, *, kind: ControlCommandKind, payload: str | None, **facts: object) -> ControlCommand:
        normalized = payload
        return ControlCommand(
            str(facts["command_id"]),
            DispatchId(dispatch_id),
            Generation(int(facts["generation"])),
            int(facts["attempt"]),
            ThreadIdentity(str(facts["thread_id"])),
            str(facts["turn_id"]),
            kind,
            normalized,
            hashlib.sha256((normalized or "").encode()).hexdigest(),
        )

    def steer(self, dispatch_id: str, **facts: object) -> ControlCommand:
        self.calls.append((dispatch_id, facts))
        return self._command(
            dispatch_id,
            kind=ControlCommandKind.STEER,
            payload=str(facts["text"]),
            **{key: value for key, value in facts.items() if key != "text"},
        )

    def interrupt(self, dispatch_id: str, **facts: object) -> ControlCommand:
        self.calls.append((dispatch_id, facts))
        return self._command(dispatch_id, kind=ControlCommandKind.INTERRUPT, payload=None, **facts)

    def command_status(self, _command_id: str) -> ControlCommand | None:
        return None


class _HistoryTerminalLiveClient(_TerminalUiLiveClient):
    def __init__(self) -> None:
        super().__init__()
        self.history_calls: list[dict[str, object]] = []

    def conversation_history(self, **facts: object) -> ConversationHistoryPage:
        self.history_calls.append(facts)
        older = facts.get("page_token") is not None
        fragment = ConversationMessageFragment(
            "turn-1" if older else "turn-2",
            "user-1" if older else "agent-2",
            ConversationSpeaker.USER if older else ConversationSpeaker.AGENT,
            ConversationContent(ConversationContentKind.TEXT, "older question" if older else "newest answer"),
            0 if older else 1,
        )
        return ConversationHistoryPage(
            ConversationSubjectKind(str(facts["subject_kind"])),
            str(facts["subject_id"]),
            ThreadIdentity(str(facts["thread_id"])),
            ConversationHistoryStatus.AVAILABLE,
            "stable-snapshot",
            (ConversationTurnSlice(fragment.turn_id, 0 if older else 1, (fragment,)),),
            None if older else "older-token",
        )


class _TerminalUiDecisionClient:
    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.submitted: ModelFacingControllerActionBundle | None = None
        self.claimant_id: str | None = None

    def pending(self) -> tuple[ControllerDecisionStatus, ...]:
        if not self.online:
            raise ControlClientError("supervisor control endpoint is unavailable")
        return (_tui_decision(),)

    def claim(self, decision_id: str, **facts: object) -> ControllerDecisionClaim:
        assert decision_id == str(_tui_decision().decision_id)
        assert isinstance(facts["claimant_id"], str) and str(facts["claimant_id"]).startswith("terminal-ui-")
        assert facts["expected_revision"] == 3
        assert facts["generation"] == 1
        assert facts["claimant_kind"] is ControllerClaimantKind.HUMAN
        self.claimant_id = str(facts["claimant_id"])
        return ControllerDecisionClaim(
            ControllerDecisionId(decision_id),
            Generation(1),
            ControllerClaimantKind.HUMAN,
            self.claimant_id,
            4,
            "2026-08-30T11:00:00Z",
            "secret-memory-only-token",
        )

    def submit_actions(self, bundle: ModelFacingControllerActionBundle, **facts: object) -> ControllerActionReceipt:
        assert facts == {"claimant_id": self.claimant_id, "token": "secret-memory-only-token"}
        self.submitted = bundle
        return ControllerActionReceipt(
            bundle.action_id,
            bundle.decision_id,
            bundle.generation,
            bundle.expected_revision,
            bundle.sha256,
            {"applied_actions": [bundle.actions[0].kind.value]},
            "committed",
            "2026-08-30T10:00:00Z",
        )

    def acknowledge(self, decision_id: str, **facts: object) -> ControllerActionReceipt:
        assert self.submitted is not None
        assert facts["committed_revision"] == self.submitted.expected_revision + 1
        assert facts["token"] == "secret-memory-only-token"
        return ControllerActionReceipt(
            facts["action_id"],  # type: ignore[arg-type]
            ControllerDecisionId(decision_id),
            self.submitted.generation,
            self.submitted.expected_revision,
            facts["bundle_sha256"],  # type: ignore[arg-type]
            {"applied_actions": [self.submitted.actions[0].kind.value]},
            "acknowledged",
            "2026-08-30T10:00:00Z",
            "2026-08-30T10:00:01Z",
        )


class _AmbiguousTerminalLiveClient(_TerminalUiLiveClient):
    def __init__(self, behavior: str) -> None:
        super().__init__()
        self.behavior = behavior
        self.status_calls: list[str] = []
        self.command: ControlCommand | None = None

    def steer(self, dispatch_id: str, **facts: object) -> ControlCommand:
        self.calls.append((dispatch_id, facts))
        command = self._command(
            dispatch_id,
            kind=ControlCommandKind.STEER,
            payload=str(facts["text"]),
            **{key: value for key, value in facts.items() if key != "text"},
        )
        if self.behavior == "rejected":
            raise ControlCommandRejected("explicit rejection")
        if self.behavior == "lost_commit" and self.command is None:
            self.command = command
            raise ControlCommandPostSendUncertain("reply lost")
        if self.behavior == "absent_then_success" and len(self.calls) == 1:
            raise ControlCommandPostSendUncertain("reply lost before commit")
        if self.behavior == "repeated_ambiguity":
            raise ControlCommandPostSendUncertain("reply lost")
        if self.behavior == "mismatch":
            raise ControlCommandPostSendUncertain("reply lost")
        self.command = command
        return command

    def command_status(self, command_id: str) -> ControlCommand | None:
        self.status_calls.append(command_id)
        if self.behavior == "repeated_ambiguity":
            raise ControlClientError("status unavailable")
        if self.behavior == "mismatch":
            assert self.calls
            dispatch_id, facts = self.calls[0]
            return self._command(
                dispatch_id,
                kind=ControlCommandKind.STEER,
                payload="different semantics",
                **{key: value for key, value in facts.items() if key != "text"},
            )
        return self.command


def test_terminal_ui_models_are_typed_bounded_and_truthful_about_unexposed_facts() -> None:
    view = WorkerView.from_status(_tui_worker(), now=datetime(2026, 8, 30, 10, 0, 9, tzinfo=UTC))
    assert (view.run_id, view.milestone_id, view.role) == ("run", "milestone", "executor")
    assert (view.thread_id, view.turn_id, view.elapsed, view.activity_age) == (
        "thread-1",
        "turn-1",
        "not exposed by control API",
        "9s",
    )
    assert view.model == "not exposed by control API"
    assert view.effort == "not exposed by control API"
    assert len(view.activity) == 1


def test_terminal_ui_explicit_refresh_preserves_offline_snapshot_and_rejects_mutation() -> None:
    live = _TerminalUiLiveClient()
    decisions = _TerminalUiDecisionClient()
    client = TerminalUiClient(live, decisions)  # type: ignore[arg-type]

    async def scenario() -> None:
        online = await client.refresh()
        assert online.connected and len(online.workers) == len(online.decisions) == 1
        live.online = decisions.online = False
        offline = await client.refresh()
        assert not offline.connected
        assert offline.workers == online.workers and offline.decisions == online.decisions
        with pytest.raises(TerminalUiOfflineError, match="read-only"):
            await client.interrupt(_tui_worker())

    asyncio.run(scenario())


def test_decision_resume_requires_one_current_connected_inactive_source_thread() -> None:
    inactive = LiveWorkerStatus(
        DispatchId(DISPATCH),
        "running",
        1,
        2,
        ThreadIdentity("thread-1"),
        None,
        RetryPolicyFacts(),
        (),
    )

    class ResumeLive(_TerminalUiLiveClient):
        def __init__(self, statuses: tuple[LiveWorkerStatus, ...]) -> None:
            super().__init__()
            self.statuses = statuses

        def status(self) -> tuple[LiveWorkerStatus, ...]:
            if not self.online:
                raise ControlClientError("supervisor control endpoint is unavailable")
            return self.statuses

    async def scenario() -> None:
        live = ResumeLive((inactive,))
        decisions = _TerminalUiDecisionClient()
        client = TerminalUiClient(live, decisions)  # type: ignore[arg-type]
        await client.refresh()
        decision_id = str(_tui_decision().decision_id)
        assert client.open_resume_thread_id(worker_id=DISPATCH, decision_id=None) == "thread-1"
        assert client.open_resume_thread_id(worker_id=None, decision_id=decision_id) == "thread-1"
        with pytest.raises(ValueError, match="exactly one"):
            client.open_resume_thread_id(worker_id=DISPATCH, decision_id=decision_id)
        with pytest.raises(ValueError, match="exactly one"):
            client.open_resume_thread_id(worker_id=None, decision_id=None)

        live.statuses = (_tui_worker(),)
        await client.refresh()
        with pytest.raises(ValueError, match="inactive-turn proof"):
            client.open_resume_thread_id(worker_id=None, decision_id=decision_id)

        duplicate = LiveWorkerStatus(
            DispatchId("run/milestone/reviewer/1"),
            "running",
            1,
            1,
            ThreadIdentity("thread-1"),
            None,
            RetryPolicyFacts(),
            (),
        )
        live.statuses = (inactive, duplicate)
        await client.refresh()
        with pytest.raises(ValueError, match="one current"):
            client.open_resume_thread_id(worker_id=None, decision_id=decision_id)

        live.statuses = ()
        await client.refresh()
        with pytest.raises(ValueError, match="one current"):
            client.open_resume_thread_id(worker_id=None, decision_id=decision_id)

        live.online = decisions.online = False
        await client.refresh()
        with pytest.raises(TerminalUiOfflineError, match="read-only"):
            client.open_resume_thread_id(worker_id=None, decision_id=decision_id)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("behavior", "outcome", "send_count", "status_count"),
    (
        ("lost_commit", "accepted", 1, 1),
        ("absent_then_success", "accepted", 2, 1),
        ("rejected", "rejected", 1, 0),
    ),
)
def test_terminal_ui_control_retains_one_id_through_reconciliation(
    behavior: str, outcome: str, send_count: int, status_count: int
) -> None:
    live = _AmbiguousTerminalLiveClient(behavior)
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        result = await client.steer(client.worker_status(DISPATCH), "same semantic request")
        assert result.outcome == outcome
        assert len(live.calls) == send_count
        assert len(live.status_calls) == status_count
        command_ids = {str(facts["command_id"]) for _, facts in live.calls}
        assert command_ids == {result.identity}

    asyncio.run(scenario())


def test_terminal_ui_repeated_ambiguity_never_resends_or_allocates_another_id() -> None:
    live = _AmbiguousTerminalLiveClient("repeated_ambiguity")
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        first = await client.steer(client.worker_status(DISPATCH), "same semantic request")
        second = await client.steer(client.worker_status(DISPATCH), "same semantic request")
        assert first.outcome == second.outcome == "post_send_uncertain"
        assert first.identity == second.identity
        assert len(live.calls) == 1
        assert live.status_calls == [first.identity, first.identity]

    asyncio.run(scenario())


def test_terminal_ui_durable_command_semantic_mismatch_fails_closed() -> None:
    live = _AmbiguousTerminalLiveClient("mismatch")
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        with pytest.raises(RuntimeError, match="retained semantic identity"):
            await client.steer(client.worker_status(DISPATCH), "expected semantics")
        assert len(live.calls) == len(live.status_calls) == 1

    asyncio.run(scenario())


def test_terminal_ui_binds_exact_turn_and_replacement_bound_decision_actions() -> None:
    live = _TerminalUiLiveClient()
    decisions = _TerminalUiDecisionClient()
    client = TerminalUiClient(live, decisions)  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        await client.steer(_tui_worker(), "continue with the bounded proof")
        dispatch_id, facts = live.calls[-1]
        assert dispatch_id == DISPATCH
        assert facts | {"command_id": "<generated>"} == {
            "generation": 1,
            "attempt": 2,
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "text": "continue with the bounded proof",
            "command_id": "<generated>",
        }
        assert isinstance(facts["command_id"], str) and facts["command_id"]
        decision = _tui_decision()
        await client.human_claim(decision)
        with pytest.raises(ValueError, match="visible decision revision"):
            await client.rearm_checkpoint(decision, checkpoint_seconds=900)
        claimed = ControllerDecisionStatus(
            decision.decision_id,
            decision.dispatch_id,
            decision.kind,
            ControllerDecisionState.CLAIMED,
            4,
            decision.current_generation,
            decision.generation_budget,
            decision.generation_used,
            ControllerClaimantKind.HUMAN,
            decisions.claimant_id,
            "2026-08-30T11:00:00Z",
            None,
            None,
            decision.deadline,
            decision.summary,
        )
        result = await client.rearm_checkpoint(claimed, checkpoint_seconds=900)
        assert result.detail == "acknowledged"
        assert decisions.submitted is not None
        assert decisions.submitted.decision_id == decision.decision_id
        assert decisions.submitted.expected_revision == 4
        assert decisions.submitted.actions[0].kind is ControllerActionKind.REARM_CHECKPOINT
        assert decisions.submitted.actions[0].checkpoint_seconds == 900
        assert "secret-memory-only-token" not in repr(client.snapshot)

    asyncio.run(scenario())


def test_terminal_ui_loads_and_prepends_complete_history_without_polling() -> None:
    live = _HistoryTerminalLiveClient()
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        first = await client.load_conversation(worker_id=DISPATCH)
        assert first.older_token == "older-token"
        older = await client.load_conversation(worker_id=DISPATCH, older=True)
        assert older.older_token is None
        pages = client.conversation_pages(DISPATCH)
        assert [message.content.text for page in pages for turn in page.turns for message in turn.messages] == [
            "older question",
            "newest answer",
        ]
        assert [call["page_token"] for call in live.history_calls] == [None, "older-token"]
        assert all(call["thread_id"] == "thread-1" for call in live.history_calls)

    asyncio.run(scenario())


def test_terminal_ui_discards_history_on_disconnect_and_identity_replacement() -> None:
    class ReplaceableHistoryClient(_HistoryTerminalLiveClient):
        thread_id = "thread-1"

        def status(self) -> tuple[LiveWorkerStatus, ...]:
            if not self.online:
                raise ControlClientError("supervisor control endpoint is unavailable")
            status = _tui_worker()
            return (
                LiveWorkerStatus(
                    status.dispatch_id,
                    status.state,
                    status.generation,
                    status.attempt,
                    ThreadIdentity(self.thread_id),
                    status.active_turn_id,
                    status.retry_policy,
                    status.recent_activity,
                ),
            )

    live = ReplaceableHistoryClient()
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        await client.refresh()
        await client.load_conversation(worker_id=DISPATCH)
        assert client.conversation_pages(DISPATCH)
        live.online = False
        await client.refresh()
        assert client.conversation_pages(DISPATCH) == ()
        live.online = True
        await client.refresh()
        await client.load_conversation(worker_id=DISPATCH)
        live.thread_id = "thread-replacement"
        await client.refresh()
        assert client.conversation_pages(DISPATCH) == ()

    asyncio.run(scenario())


def test_terminal_ui_load_key_targets_focused_controller_conversation() -> None:
    live = _HistoryTerminalLiveClient()
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        terminal = CodexFlowTerminalApp(client)
        async with terminal.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            decisions = terminal.query_one("#decisions")
            terminal.set_focus(decisions)
            await pilot.press("l")
            await pilot.pause()
            assert live.history_calls[-1]["subject_kind"] == "controller"
            assert live.history_calls[-1]["subject_id"] == str(_tui_decision().decision_id)
            assert "CONTROLLER CONVERSATION" in str(terminal.query_one("#conversation").render())
            assert terminal.query_one("#conversation").size.height > 0
            assert terminal.query_one("#actions").size.height > 0

    asyncio.run(scenario())


def test_terminal_ui_reassembles_one_message_split_across_pages() -> None:
    older = ConversationMessageFragment(
        "turn-1",
        "user-1",
        ConversationSpeaker.USER,
        ConversationContent(ConversationContentKind.TEXT, "first "),
        0,
    )
    newer = ConversationMessageFragment(
        "turn-1",
        "user-1",
        ConversationSpeaker.USER,
        ConversationContent(ConversationContentKind.TEXT, "second"),
        1,
    )
    pages = (
        ConversationHistoryPage(
            ConversationSubjectKind.WORKER,
            DISPATCH,
            ThreadIdentity("thread-1"),
            ConversationHistoryStatus.AVAILABLE,
            "snapshot",
            (ConversationTurnSlice("turn-1", 0, (older,)),),
        ),
        ConversationHistoryPage(
            ConversationSubjectKind.WORKER,
            DISPATCH,
            ThreadIdentity("thread-1"),
            ConversationHistoryStatus.AVAILABLE,
            "snapshot",
            (ConversationTurnSlice("turn-1", 0, (newer,)),),
        ),
    )

    rendered = CodexFlowTerminalApp._conversation_messages(pages)

    assert rendered == ("USER · text\nfirst second",)


def test_terminal_ui_driver_confirms_interrupt_and_close_has_no_lifecycle_effect() -> None:
    live = _TerminalUiLiveClient()
    decisions = _TerminalUiDecisionClient()
    client = TerminalUiClient(live, decisions)  # type: ignore[arg-type]

    async def scenario() -> None:
        terminal = CodexFlowTerminalApp(client)
        async with terminal.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "Send direction" in str(terminal.query_one("#actions").render())
            await pilot.press("s")
            await pilot.pause()
            steer_editor = terminal.screen
            assert isinstance(steer_editor, SteerActionScreen)
            assert "turn-1" in str(steer_editor.query("Static")[1].render())
            steer_editor.dismiss(None)
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            confirmation = terminal.screen
            assert isinstance(confirmation, ConfirmActionScreen)
            assert DISPATCH in str(confirmation.query("Static")[1].render())
            confirmation.dismiss(False)
            await pilot.pause()
            terminal.query_one("#decisions").index = 0  # type: ignore[attr-defined]
            await pilot.press("k")
            await pilot.pause()
            claim_confirmation = terminal.screen
            assert isinstance(claim_confirmation, ConfirmActionScreen)
            assert str(_tui_decision().decision_id) in str(claim_confirmation.query("Static")[1].render())
            claim_confirmation.dismiss(False)
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            rearm_confirmation = terminal.screen
            assert isinstance(rearm_confirmation, ConfirmActionScreen)
            assert "30 minute" in str(rearm_confirmation.query_one("#confirm-title").render())
            rearm_confirmation.dismiss(False)
            await pilot.pause()
            live.online = decisions.online = False
            await pilot.press("r")
            await pilot.pause()
            terminal.query_one("#decisions").index = 0  # type: ignore[attr-defined]
            await pilot.press("k")
            await pilot.pause()
            assert "Rejected" in str(terminal.query_one("#feedback").render())
        assert live.calls == []

    asyncio.run(scenario())


def test_terminal_ui_open_uses_focused_selection_and_preserves_exact_thread_argument() -> None:
    selected_worker = LiveWorkerStatus(
        DispatchId(DISPATCH),
        "running",
        1,
        2,
        ThreadIdentity("thread-worker"),
        None,
        RetryPolicyFacts(),
        (),
    )
    decision_source = LiveWorkerStatus(
        DispatchId("run/milestone/reviewer/1"),
        "running",
        1,
        1,
        ThreadIdentity("thread-1"),
        None,
        RetryPolicyFacts(),
        (),
    )

    class InactiveLive(_TerminalUiLiveClient):
        def status(self) -> tuple[LiveWorkerStatus, ...]:
            return (selected_worker, decision_source)

    client = TerminalUiClient(InactiveLive(), _TerminalUiDecisionClient())  # type: ignore[arg-type]
    opened: list[str] = []

    async def scenario() -> None:
        terminal = CodexFlowTerminalApp(client, resume_handler=lambda value: opened.append(value) or "opened")
        async with terminal.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            workers = terminal.query_one("#workers")
            decisions = terminal.query_one("#decisions")
            assert workers.index == decisions.index == 0  # type: ignore[attr-defined]

            terminal.set_focus(workers)
            await pilot.press("o")
            await pilot.pause()
            assert opened == ["thread-worker"]
            assert workers.index == decisions.index == 0  # type: ignore[attr-defined]

            terminal.set_focus(decisions)
            await pilot.press("o")
            await pilot.pause()
            assert opened == ["thread-worker", "thread-1"]
            assert workers.index == decisions.index == 0  # type: ignore[attr-defined]
            assert "Progress check" in str(terminal.query_one("#conversation-header").render())
            workers.index = 1  # type: ignore[attr-defined]
            await pilot.pause()
            assert "Progress check" in str(terminal.query_one("#conversation-header").render())
            workers.index = 0  # type: ignore[attr-defined]

            terminal.set_focus(None)
            await pilot.press("o")
            await pilot.pause()
            assert opened == ["thread-worker", "thread-1"]
            assert "focus a worker or decision" in str(terminal.query_one("#feedback").render())

            workers.index = None  # type: ignore[attr-defined]
            terminal.set_focus(workers)
            await pilot.press("o")
            await pilot.pause()
            assert opened == ["thread-worker", "thread-1"]
            assert "no selected worker" in str(terminal.query_one("#feedback").render())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("behavior", "label"),
    (("rejected", "Rejected"), ("repeated_ambiguity", "Post Send Uncertain")),
)
def test_terminal_ui_renders_rejected_and_post_send_uncertain_distinctly(behavior: str, label: str) -> None:
    live = _AmbiguousTerminalLiveClient(behavior)
    client = TerminalUiClient(live, _TerminalUiDecisionClient())  # type: ignore[arg-type]

    async def scenario() -> None:
        terminal = CodexFlowTerminalApp(client)
        async with terminal.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            terminal._run_mutation(client.steer(client.worker_status(DISPATCH), "bounded feedback"))
            await pilot.pause()
            assert label in str(terminal.query_one("#feedback").render())

    asyncio.run(scenario())


def test_terminal_ui_reaches_real_local_supervisor_socket_without_provider_or_app_calls(tmp_path: Path) -> None:
    supervisor = Supervisor(tmp_path, worker_command=("provider-must-not-run",))
    ledger = supervisor.ledger
    ledger.create_run("run")
    ledger.create_milestone("run", "milestone")
    ledger.claim_dispatch("run", "milestone", "executor", 1)
    ledger.enqueue_dispatch(
        DISPATCH,
        backend="sdk_headless",
        capsule_json='{"model":"not-run","prompt":"provider-free"}',
        route_json='{"leaf_worker_policy":{"agents.enabled":false,"features.multi_agent":false,"config_overrides":[]}}',
        workspace_path=tmp_path,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    authority = supervisor.acquire()
    epoch = int(authority["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    ledger._db().execute("UPDATE dispatch_queue SET thread_id = ? WHERE dispatch_id = ?", ("thread-1", DISPATCH))
    ledger._db().commit()
    ledger.set_queue_state(DISPATCH, "running", epoch=epoch)
    ledger.append_diagnostic(DISPATCH, kind="provider_free", text="synthetic turn bound through real IPC")
    supervisor._active_turns[DISPATCH] = (1, 1, "thread-1", "turn-1")
    endpoint = supervisor._socket
    assert endpoint is not None
    endpoint.setblocking(True)

    def serve_exact_requests() -> None:
        for _ in range(3):
            connection, _ = endpoint.accept()
            supervisor._accept_connection(connection)

    service = threading.Thread(target=serve_exact_requests, name="provider-free-tui-supervisor")
    service.start()
    client = TerminalUiClient.for_state_root(tmp_path)

    async def scenario() -> None:
        snapshot = await client.refresh()
        assert snapshot.connected and snapshot.workers[0].dispatch_id == DISPATCH
        result = await client.steer(client.worker_status(DISPATCH), "provider-free steer")
        assert result.operation == "steer"

    asyncio.run(scenario())
    service.join(timeout=2)
    assert not service.is_alive()
    commands = ledger.control_commands(DISPATCH)
    assert len(commands) == 1 and commands[0]["payload"] == "provider-free steer"
    assert supervisor._children == {}
    supervisor.close()


def test_inactive_history_read_is_read_only_ephemeral_and_identity_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(tmp_path, worker_command=("provider-must-not-run",))
    ledger = supervisor.ledger
    ledger.create_run("run")
    ledger.create_milestone("run", "milestone")
    ledger.claim_dispatch("run", "milestone", "executor", 1)
    ledger.enqueue_dispatch(
        DISPATCH,
        backend="sdk_headless",
        capsule_json='{"model":"test","reasoning_effort":"medium","prompt":"bounded"}',
        route_json='{"leaf_worker_policy":{"agents.enabled":false,"features.multi_agent":false,"config_overrides":[]}}',
        workspace_path=tmp_path,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    ledger._db().execute("UPDATE dispatch_queue SET thread_id = ? WHERE dispatch_id = ?", ("thread-1", DISPATCH))
    ledger._db().commit()
    row = ledger.queue_dispatch(DISPATCH)
    observed: list[tuple[Sandbox, str]] = []

    def read_history(adapter: CodexSdkAdapter, request: object) -> ConversationHistoryPage:
        assert isinstance(request, object)
        observed.append((adapter.config.sandbox, adapter.config.model))
        return ConversationHistoryPage(
            ConversationSubjectKind.WORKER,
            DISPATCH,
            ThreadIdentity("thread-1"),
            ConversationHistoryStatus.AVAILABLE,
            "stable-snapshot",
        )

    monkeypatch.setattr(CodexSdkAdapter, "read_conversation_history", read_history)
    request = {
        "version": 1,
        "operation": "conversation_history",
        "subject_kind": "worker",
        "subject_id": DISPATCH,
        "thread_id": "thread-1",
        "generation": row["generation"],
        "attempt": row["attempt"],
        "revision": None,
        "page_token": None,
        "page_fragments": 32,
    }
    before = "\n".join(ledger._db().iterdump())

    response = supervisor._conversation_history(request)

    assert response["page"]["status"] == "available"  # type: ignore[index]
    assert observed == [(Sandbox.READ_ONLY, "test")]
    assert "\n".join(ledger._db().iterdump()) == before
    request["generation"] = int(row["generation"]) + 1
    with pytest.raises(IpcError, match="process identity is stale"):
        supervisor._conversation_history(request)
    supervisor.close()


def test_history_broker_limits_slow_reads_and_returns_at_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = Supervisor(tmp_path, worker_command=("provider-must-not-run",))
    monkeypatch.setattr("codex_flow.supervisor.CONVERSATION_READ_DEADLINE_SECONDS", 0.01)
    release = threading.Event()
    all_finished = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def slow_read(_request: object) -> ConversationHistoryPage:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(2)
        with lock:
            active -= 1
            if active == 0:
                all_finished.set()
        return ConversationHistoryPage(
            ConversationSubjectKind.WORKER,
            DISPATCH,
            ThreadIdentity("thread-1"),
            ConversationHistoryStatus.AVAILABLE,
            "stable-snapshot",
        )

    monkeypatch.setattr(supervisor, "_inactive_conversation_page", slow_read)
    request = {
        "version": 1,
        "operation": "conversation_history",
        "subject_kind": "worker",
        "subject_id": DISPATCH,
        "thread_id": "thread-1",
        "generation": 1,
        "attempt": 1,
        "revision": None,
        "page_token": None,
        "page_fragments": 32,
    }
    before = "\n".join(supervisor.ledger._db().iterdump())

    responses = [supervisor._conversation_history(request) for _ in range(5)]

    assert [response["page"]["reason"] for response in responses[:4]] == [  # type: ignore[index]
        "conversation read exceeded its deadline"
    ] * 4
    assert responses[4]["page"]["reason"] == "conversation read capacity is busy"  # type: ignore[index]
    assert maximum == 4
    assert "\n".join(supervisor.ledger._db().iterdump()) == before
    release.set()
    assert all_finished.wait(2)
    supervisor.close()


def test_real_supervisor_socket_reconciles_commit_after_lost_reply_with_zero_duplicates(tmp_path: Path) -> None:
    supervisor = Supervisor(tmp_path, worker_command=("provider-must-not-run",))
    ledger = supervisor.ledger
    ledger.create_run("run")
    ledger.create_milestone("run", "milestone")
    ledger.claim_dispatch("run", "milestone", "executor", 1)
    ledger.enqueue_dispatch(
        DISPATCH,
        backend="sdk_headless",
        capsule_json='{"model":"not-run","prompt":"provider-free"}',
        route_json='{"leaf_worker_policy":{"agents.enabled":false,"features.multi_agent":false,"config_overrides":[]}}',
        workspace_path=tmp_path,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    epoch = int(supervisor.acquire()["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    ledger._db().execute("UPDATE dispatch_queue SET thread_id = ? WHERE dispatch_id = ?", ("thread-1", DISPATCH))
    ledger._db().commit()
    ledger.set_queue_state(DISPATCH, "running", epoch=epoch)
    supervisor._active_turns[DISPATCH] = (1, 1, "thread-1", "turn-1")
    endpoint = supervisor._socket
    assert endpoint is not None
    endpoint.setblocking(True)

    def serve_exact_requests() -> None:
        for _ in range(4):
            connection, _ = endpoint.accept()
            supervisor._accept_connection(connection)

    service = threading.Thread(target=serve_exact_requests, name="lost-reply-control-supervisor")
    service.start()
    command_id = "caller-retained-command"
    request = {
        "version": 1,
        "operation": "steer",
        "dispatch_id": DISPATCH,
        "generation": 1,
        "attempt": 1,
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "command_id": command_id,
        "payload": "provider-free retained command",
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(supervisor.socket_path))
        connection.sendall(encode_frame(request))
        # Close without reading: the supervisor has no reply consumer, while
        # its commit remains the canonical fact reconciled below.

    client = LiveWorkerControlClient.for_state_root(tmp_path)
    committed = client.command_status(command_id)
    assert committed is not None and committed.state is ControlCommandState.PENDING
    replay = client.steer(
        DISPATCH,
        generation=1,
        attempt=1,
        thread_id="thread-1",
        turn_id="turn-1",
        text="provider-free retained command",
        command_id=command_id,
    )
    assert replay.command_id == command_id
    claimed = ledger.claim_control_commands(
        DISPATCH,
        generation=1,
        attempt=1,
        thread_id="thread-1",
        turn_id="turn-1",
    )
    assert len(claimed) == 1
    ledger.acknowledge_control_command(
        command_id,
        state=ControlCommandState.ACKNOWLEDGED,
        acknowledgement={"detail": "applied once"},
    )
    terminal = client.command_status(command_id)
    assert terminal is not None and terminal.state is ControlCommandState.ACKNOWLEDGED
    assert len(ledger.control_commands(DISPATCH)) == 1
    assert supervisor._children == {}
    service.join(timeout=2)
    assert not service.is_alive()
    supervisor.close()
