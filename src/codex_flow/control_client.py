"""Typed, local control-plane client for live detached workers."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from .contracts import ModelFacingControllerActionBundle
from .domain import (
    CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES,
    CompatibilityRebind,
    ControlCommand,
    ControllerActionReceipt,
    ControllerClaimantKind,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerDecisionStatus,
    ControllerGenerationStatus,
    ControllerRecoveryInspectionClaim,
    ConversationHistoryPage,
    DiagnosticEvent,
    DispatchId,
    Generation,
    LiveWorkerActivity,
    LiveWorkerStatus,
    RecoveryAction,
    RecoveryActionKind,
    RetryBudgetChange,
    RetryPolicyFacts,
    ThreadIdentity,
    conversation_history_page_from_json,
    strict_json_loads,
)
from .ipc import IpcError, send_request


class ControlClientError(RuntimeError):
    """The supervisor rejected a typed control-plane request."""


class ControlCommandRejected(ControlClientError):
    """The supervisor explicitly rejected a live-control command."""


class ControlCommandPostSendUncertain(ControlClientError):
    """A live-control transport failed after the command may have been sent."""


_RESPONSE_FIELDS = {
    "status": {"version", "ok", "queue"},
    "activity": {"version", "ok", "dispatch_id", "activity"},
    "steer": {"version", "ok", "command"},
    "interrupt": {"version", "ok", "command"},
    "control_status": {"version", "ok", "command"},
    "recovery_action": {"version", "ok", "action"},
    "controller_pending": {"version", "ok", "decisions"},
    "controller_status": {"version", "ok", "decision"},
    "controller_claim": {"version", "ok", "claim"},
    "controller_renew_claim": {"version", "ok", "claim"},
    "controller_defer_rate_limit": {"version", "ok", "claim"},
    "controller_submit_actions": {"version", "ok", "receipt"},
    "controller_acknowledge": {"version", "ok", "receipt"},
    "controller_submit_recovered_actions": {"version", "ok", "receipt"},
    "controller_acknowledge_recovered": {"version", "ok", "receipt"},
    "controller_recover": {"version", "ok", "generation"},
    "controller_reserve_recovery": {"version", "ok", "claim"},
    "controller_complete_recovery": {"version", "ok", "generation"},
    "controller_generation": {"version", "ok", "generation"},
    "controller_prepare_generation": {"version", "ok", "generation"},
    "controller_bind_generation_thread": {"version", "ok", "generation"},
    "controller_bind_generation_turn": {"version", "ok", "generation"},
    "controller_complete_generation": {"version", "ok", "generation"},
    "controller_reset_generation_delivery": {"version", "ok", "generation"},
    "conversation_history": {"version", "ok", "page"},
}


def _response(response: object, *, operation: str) -> dict[str, object]:
    expected = _RESPONSE_FIELDS.get(operation)
    if (
        expected is None
        or not isinstance(response, dict)
        or set(response) != expected
        or response.get("version") != 1
        or response.get("ok") is not True
    ):
        raise ControlClientError("control request rejected")
    return response


def _shape(value: object, required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != required:
        raise ControlClientError("supervisor returned an unsupported typed shape")
    return value


def _decode_event(value: object) -> DiagnosticEvent:
    raw = _shape(value, {"sequence", "kind", "occurred_at", "text", "payload_sha256"})
    try:
        return DiagnosticEvent(
            sequence=raw["sequence"],  # type: ignore[arg-type]
            kind=raw["kind"],  # type: ignore[arg-type]
            occurred_at=raw["occurred_at"],  # type: ignore[arg-type]
            text=raw["text"],  # type: ignore[arg-type]
            payload_sha256=raw["payload_sha256"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed activity") from exc


def _decode_conversation_page(value: object) -> ConversationHistoryPage:
    try:
        return conversation_history_page_from_json(value)
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed conversation page") from exc


def _decode_retry_policy(value: object) -> RetryPolicyFacts:
    fields = {
        "revision",
        "policy_version",
        "pre_identity_budget",
        "invalid_chain_budget",
        "schema_envelope_budget",
        "post_identity_loss_budget",
        "pre_identity_used",
        "invalid_chain_used",
        "schema_envelope_used",
        "post_identity_loss_used",
        "last_failure",
        "strategy",
        "next_eligible_at",
        "prior_thread_id",
        "prior_turn_id",
        "human_attention_reason",
    }
    raw = _shape(value, fields)
    try:
        return RetryPolicyFacts(**raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed retry policy") from exc


def _decode_status(value: object) -> LiveWorkerStatus:
    fields = {
        "dispatch_id",
        "run_id",
        "milestone_id",
        "role",
        "generation",
        "backend",
        "workspace_path",
        "result_contract_sha256",
        "state",
        "sequence",
        "available_at",
        "deadline",
        "claim_epoch",
        "attempt",
        "thread_id",
        "host_id",
        "raw_result_sha256",
        "terminal_status",
        "created_at",
        "updated_at",
        "retry_policy",
        "recent_activity",
        "active_turn_id",
    }
    raw = _shape(value, fields)
    # Validate the complete wire envelope, including fields not projected into
    # the semantic model.  A closed decoder must reject a wrongly typed value
    # before it can be silently discarded at the public boundary.
    string_fields = (
        "run_id",
        "milestone_id",
        "role",
        "backend",
        "workspace_path",
        "result_contract_sha256",
        "state",
        "available_at",
        "deadline",
        "host_id",
        "raw_result_sha256",
        "terminal_status",
        "created_at",
        "updated_at",
    )
    nullable_string_fields = {"available_at", "deadline", "host_id", "raw_result_sha256", "terminal_status"}
    for name in string_fields:
        current = raw[name]
        if current is None and name in nullable_string_fields:
            continue
        if not isinstance(current, str):
            raise ControlClientError(f"supervisor returned malformed status field: {name}")
    integer_fields = ("generation", "sequence", "attempt", "claim_epoch")
    for name in integer_fields:
        current = raw[name]
        if current is None and name == "claim_epoch":
            continue
        if isinstance(current, bool) or not isinstance(current, int):
            raise ControlClientError(f"supervisor returned malformed status field: {name}")
    activity_raw = raw["recent_activity"]
    if not isinstance(activity_raw, list):
        raise ControlClientError("supervisor returned malformed activity")
    try:
        return LiveWorkerStatus(
            dispatch_id=DispatchId(raw["dispatch_id"]),  # type: ignore[arg-type]
            state=raw["state"],  # type: ignore[arg-type]
            generation=Generation(raw["generation"]),  # type: ignore[arg-type]
            attempt=raw["attempt"],  # type: ignore[arg-type]
            thread_id=ThreadIdentity(raw["thread_id"]) if raw["thread_id"] is not None else None,  # type: ignore[arg-type]
            active_turn_id=raw["active_turn_id"],  # type: ignore[arg-type]
            retry_policy=_decode_retry_policy(raw["retry_policy"]),
            recent_activity=tuple(_decode_event(item) for item in activity_raw),
        )
    except (TypeError, ValueError, ControlClientError) as exc:
        raise ControlClientError("supervisor returned malformed status") from exc


def _decode_command(value: object) -> ControlCommand:
    fields = {
        "command_id",
        "dispatch_id",
        "submission_sequence",
        "generation",
        "attempt",
        "thread_id",
        "turn_id",
        "kind",
        "payload",
        "payload_sha256",
        "state",
        "acknowledgement_json",
        "created_at",
        "sent_at",
        "acknowledged_at",
    }
    raw = _shape(value, fields)
    acknowledgement: str | None = None
    acknowledgement_terminal_status: str | None = None
    if raw["acknowledgement_json"] is not None:
        if not isinstance(raw["acknowledgement_json"], str):
            raise ControlClientError("supervisor returned malformed command acknowledgement")
        try:
            decoded = strict_json_loads(raw["acknowledgement_json"], max_bytes=4096)
        except (TypeError, ValueError) as exc:
            raise ControlClientError("supervisor returned malformed command acknowledgement") from exc
        if not isinstance(decoded, dict):
            raise ControlClientError("supervisor returned malformed command acknowledgement")
        allowed_shapes = ({"detail"}, {"detail", "terminal_status"})
        if set(decoded) not in allowed_shapes:
            raise ControlClientError("supervisor returned malformed command acknowledgement")
        detail = decoded["detail"]
        if detail is not None and not isinstance(detail, str):
            raise ControlClientError("supervisor returned malformed command acknowledgement")
        terminal_status = decoded.get("terminal_status")
        if terminal_status is not None and terminal_status not in CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES:
            raise ControlClientError("supervisor returned malformed command acknowledgement")
        acknowledgement = detail
        acknowledgement_terminal_status = terminal_status
    payload = raw["payload"]
    payload_sha256 = raw["payload_sha256"]
    state = raw["state"]
    if not isinstance(state, str):
        raise ControlClientError("supervisor returned malformed command state")
    if raw["acknowledgement_json"] is not None and state not in {"acknowledged", "rejected", "unresolved"}:
        raise ControlClientError("supervisor returned command acknowledgement before a terminal command state")
    if state == "acknowledged" and raw["acknowledgement_json"] is None:
        raise ControlClientError("supervisor returned acknowledged command without acknowledgement facts")
    if (
        not isinstance(payload_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
        or not isinstance(payload, str | type(None))
        or hashlib.sha256((payload or "").encode("utf-8")).hexdigest() != payload_sha256
    ):
        raise ControlClientError("supervisor returned malformed command payload digest")
    try:
        return ControlCommand(
            command_id=raw["command_id"],  # type: ignore[arg-type]
            dispatch_id=raw["dispatch_id"],  # type: ignore[arg-type]
            generation=raw["generation"],  # type: ignore[arg-type]
            attempt=raw["attempt"],  # type: ignore[arg-type]
            thread_id=raw["thread_id"],  # type: ignore[arg-type]
            turn_id=raw["turn_id"],  # type: ignore[arg-type]
            kind=raw["kind"],  # type: ignore[arg-type]
            payload=payload,
            payload_sha256=payload_sha256,
            submission_sequence=raw["submission_sequence"],  # type: ignore[arg-type]
            state=state,
            created_at=raw["created_at"],  # type: ignore[arg-type]
            sent_at=raw["sent_at"],  # type: ignore[arg-type]
            acknowledged_at=raw["acknowledged_at"],  # type: ignore[arg-type]
            acknowledgement=acknowledgement,
            acknowledgement_terminal_status=acknowledgement_terminal_status,
        )
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed control command") from exc


def _decode_action(value: object) -> RecoveryAction:
    fields = {
        "action_id",
        "dispatch_id",
        "expected_revision",
        "action_kind",
        "reason",
        "requested_budget_json",
        "compatibility_rebind_json",
        "applied_revision",
        "created_at",
    }
    raw = _shape(value, fields)
    requested: RetryBudgetChange | None = None
    if raw["requested_budget_json"] is not None:
        if not isinstance(raw["requested_budget_json"], str):
            raise ControlClientError("supervisor returned malformed recovery budget")
        try:
            requested_raw = strict_json_loads(raw["requested_budget_json"], max_bytes=1024)
            budget_fields = {
                "pre_identity_budget",
                "invalid_chain_budget",
                "schema_envelope_budget",
                "post_identity_loss_budget",
            }
            budget_values = _shape(requested_raw, budget_fields)
            requested = RetryBudgetChange(**budget_values)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ControlClientError("supervisor returned malformed recovery budget") from exc
    rebind: CompatibilityRebind | None = None
    if raw["compatibility_rebind_json"] is not None:
        if not isinstance(raw["compatibility_rebind_json"], str):
            raise ControlClientError("supervisor returned malformed compatibility rebind")
        try:
            rebind_raw = strict_json_loads(raw["compatibility_rebind_json"], max_bytes=1024)
            legacy_fields = {
                "expected_compatibility_sha256",
                "proposed_compatibility_sha256",
                "expected_generation",
                "expected_attempt",
            }
            if not isinstance(rebind_raw, dict) or set(rebind_raw) not in {
                frozenset(legacy_fields),
                frozenset(legacy_fields | {"expected_profile_sha256", "proposed_profile_sha256"}),
            }:
                raise ValueError("compatibility rebind shape is invalid")
            rebind = CompatibilityRebind(**rebind_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ControlClientError("supervisor returned malformed compatibility rebind") from exc
    try:
        return RecoveryAction(
            action_id=raw["action_id"],  # type: ignore[arg-type]
            dispatch_id=raw["dispatch_id"],  # type: ignore[arg-type]
            expected_revision=raw["expected_revision"],  # type: ignore[arg-type]
            action_kind=raw["action_kind"],  # type: ignore[arg-type]
            reason=raw["reason"],  # type: ignore[arg-type]
            requested_budget=requested,
            compatibility_rebind=rebind,
            applied_revision=raw["applied_revision"],  # type: ignore[arg-type]
            created_at=raw["created_at"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed recovery action") from exc


def _decode_controller_status(value: object) -> ControllerDecisionStatus:
    fields = {
        "decision_id",
        "dispatch_id",
        "kind",
        "state",
        "revision",
        "current_generation",
        "generation_budget",
        "generation_used",
        "claimant_kind",
        "claimant_id",
        "claim_expires_at",
        "action_id",
        "action_sha256",
        "deadline",
        "summary",
    }
    raw = _shape(value, fields)
    summary_raw = raw["summary"]
    if not isinstance(summary_raw, dict):
        raise ControlClientError("supervisor returned malformed controller summary")
    try:
        from .domain import ControllerDecisionSummary

        summary = ControllerDecisionSummary(
            DispatchId(summary_raw["dispatch_id"]),  # type: ignore[arg-type]
            summary_raw["kind"],  # type: ignore[arg-type]
            ControllerDecisionState(summary_raw["state"]),  # type: ignore[arg-type]
            summary_raw["summary"],  # type: ignore[arg-type]
            summary_raw.get("source_thread_id"),  # type: ignore[arg-type]
            summary_raw.get("deadline"),  # type: ignore[arg-type]
            summary_raw.get("revision", 0),  # type: ignore[arg-type]
            tuple(summary_raw.get("expected_successor_dispatch_ids", ())),  # type: ignore[arg-type]
            summary_raw.get("dispatch_state", "unknown"),  # type: ignore[arg-type]
            summary_raw.get("retry_policy_revision", 0),  # type: ignore[arg-type]
            summary_raw.get("retry_strategy", "none"),  # type: ignore[arg-type]
            summary_raw.get("last_failure"),  # type: ignore[arg-type]
            summary_raw.get("human_attention_reason"),  # type: ignore[arg-type]
            summary_raw.get("prior_thread_id"),  # type: ignore[arg-type]
            summary_raw.get("prior_turn_id"),  # type: ignore[arg-type]
            summary_raw.get("recovery_state", "none"),  # type: ignore[arg-type]
            summary_raw.get("worker_exit_classification"),  # type: ignore[arg-type]
            summary_raw.get("worker_exit_code"),  # type: ignore[arg-type]
            tuple(
                DiagnosticEvent(
                    event["sequence"],
                    event["kind"],
                    event["occurred_at"],
                    event.get("text"),
                    event.get("payload_sha256"),
                )
                for event in summary_raw.get("recent_activity", ())
                if isinstance(event, dict)
            ),
        )
        return ControllerDecisionStatus(
            ControllerDecisionId(raw["decision_id"]),  # type: ignore[arg-type]
            DispatchId(raw["dispatch_id"]),  # type: ignore[arg-type]
            raw["kind"],  # type: ignore[arg-type]
            ControllerDecisionState(raw["state"]),  # type: ignore[arg-type]
            raw["revision"],  # type: ignore[arg-type]
            Generation(raw["current_generation"]),  # type: ignore[arg-type]
            raw["generation_budget"],  # type: ignore[arg-type]
            raw["generation_used"],  # type: ignore[arg-type]
            ControllerClaimantKind(raw["claimant_kind"]) if raw["claimant_kind"] is not None else None,  # type: ignore[arg-type]
            raw["claimant_id"],  # type: ignore[arg-type]
            raw["claim_expires_at"],  # type: ignore[arg-type]
            raw["action_id"],  # type: ignore[arg-type]
            raw["action_sha256"],  # type: ignore[arg-type]
            raw["deadline"],  # type: ignore[arg-type]
            summary,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ControlClientError("supervisor returned malformed controller decision") from exc


def _decode_controller_claim(value: object) -> ControllerDecisionClaim:
    raw = _shape(
        value, {"decision_id", "generation", "claimant_kind", "claimant_id", "revision", "lease_expires_at", "token"}
    )
    try:
        return ControllerDecisionClaim(
            ControllerDecisionId(raw["decision_id"]),  # type: ignore[arg-type]
            Generation(raw["generation"]),  # type: ignore[arg-type]
            ControllerClaimantKind(raw["claimant_kind"]),  # type: ignore[arg-type]
            raw["claimant_id"],  # type: ignore[arg-type]
            raw["revision"],  # type: ignore[arg-type]
            raw["lease_expires_at"],  # type: ignore[arg-type]
            raw["token"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed controller claim") from exc


def _decode_controller_receipt(value: object) -> ControllerActionReceipt:
    raw = _shape(
        value,
        {
            "action_id",
            "decision_id",
            "generation",
            "expected_revision",
            "bundle_sha256",
            "effect_receipt",
            "state",
            "committed_at",
            "acknowledged_at",
        },
    )
    if not isinstance(raw["effect_receipt"], dict):
        raise ControlClientError("supervisor returned malformed controller receipt")
    try:
        return ControllerActionReceipt(
            raw["action_id"],  # type: ignore[arg-type]
            ControllerDecisionId(raw["decision_id"]),  # type: ignore[arg-type]
            Generation(raw["generation"]),  # type: ignore[arg-type]
            raw["expected_revision"],  # type: ignore[arg-type]
            raw["bundle_sha256"],  # type: ignore[arg-type]
            raw["effect_receipt"],  # type: ignore[arg-type]
            raw["state"],  # type: ignore[arg-type]
            raw["committed_at"],  # type: ignore[arg-type]
            raw["acknowledged_at"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ControlClientError("supervisor returned malformed controller receipt") from exc


class LiveWorkerControlClient:
    """One capability-scoped client; public methods return semantic models."""

    def __init__(self, socket_path: Path, *, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        if timeout <= 0:
            raise ValueError("control client timeout must be positive")
        self.timeout = timeout

    @classmethod
    def for_state_root(cls, state_root: Path, *, timeout: float = 5.0) -> LiveWorkerControlClient:
        return cls(Path(state_root).resolve() / ".codex-flow" / "runtime" / "supervisor.sock", timeout=timeout)

    def _request(self, operation: str, **facts: object) -> dict[str, object]:
        if not isinstance(operation, str) or not operation or any(c.isspace() for c in operation):
            raise ValueError("control operation is invalid")
        try:
            return _response(
                send_request(self.socket_path, {"version": 1, "operation": operation, **facts}, timeout=self.timeout),
                operation=operation,
            )
        except (IpcError, OSError) as exc:
            raise ControlClientError("supervisor control endpoint is unavailable") from exc

    def _send_control(self, operation: str, **facts: object) -> dict[str, object]:
        """Send one mutation while preserving rejection versus reply ambiguity."""

        try:
            response = send_request(
                self.socket_path,
                {"version": 1, "operation": operation, **facts},
                timeout=self.timeout,
            )
        except (IpcError, OSError) as exc:
            raise ControlCommandPostSendUncertain(
                "live-control reply was not observed; durable command state is uncertain"
            ) from exc
        if response == {"version": 1, "ok": False, "error": "request_rejected"}:
            raise ControlCommandRejected("supervisor explicitly rejected the live-control command")
        try:
            return _response(response, operation=operation)
        except ControlClientError as exc:
            raise ControlCommandPostSendUncertain(
                "live-control reply was malformed; durable command state is uncertain"
            ) from exc

    def status(self, dispatch_id: str | None = None) -> tuple[LiveWorkerStatus, ...]:
        if dispatch_id is not None:
            try:
                DispatchId(dispatch_id)
            except ValueError as exc:
                raise ValueError("dispatch id is invalid") from exc
        facts: dict[str, object] = {}
        if dispatch_id is not None:
            facts["dispatch_id"] = dispatch_id
        response = self._request("status", **facts)
        queue = response.get("queue")
        if not isinstance(queue, list):
            raise ControlClientError("supervisor returned malformed status list")
        return tuple(_decode_status(item) for item in queue)

    def conversation_history(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        thread_id: str,
        generation: int | None = None,
        attempt: int | None = None,
        revision: int | None = None,
        page_token: str | None = None,
        page_fragments: int = 32,
    ) -> ConversationHistoryPage:
        """Fetch one explicit bounded conversation page; never polls."""

        from .domain import ConversationHistoryRequest, ConversationSubjectKind

        try:
            request = ConversationHistoryRequest(
                ConversationSubjectKind(subject_kind),
                subject_id,
                ThreadIdentity(thread_id),
                generation,
                attempt,
                revision,
                page_token,
                page_fragments,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("conversation history request is invalid") from exc
        facts = {
            "subject_kind": request.subject_kind.value,
            "subject_id": request.subject_id,
            "thread_id": request.thread_id.id,
            "generation": request.generation,
            "attempt": request.attempt,
            "revision": request.revision,
            "page_token": request.page_token,
            "page_fragments": request.page_fragments,
        }
        # Null optional values remain in this closed request shape so the
        # supervisor can distinguish an omitted page token from a malformed
        # one without inferring identity.
        return _decode_conversation_page(self._request("conversation_history", **facts)["page"])

    def recent_activity(self, dispatch_id: str, *, limit: int = 128) -> LiveWorkerActivity:
        try:
            DispatchId(dispatch_id)
        except ValueError as exc:
            raise ValueError("dispatch id is invalid") from exc
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 128:
            raise ValueError("activity limit is invalid")
        response = self._request("activity", dispatch_id=dispatch_id, limit=limit)
        activity = response.get("activity")
        if not isinstance(activity, list):
            raise ControlClientError("supervisor returned malformed activity list")
        try:
            return LiveWorkerActivity(DispatchId(dispatch_id), tuple(_decode_event(item) for item in activity))
        except (TypeError, ValueError, ControlClientError) as exc:
            raise ControlClientError("supervisor returned malformed activity") from exc

    @staticmethod
    def _validate_command_id(command_id: str | None) -> str:
        value = uuid.uuid4().hex if command_id is None else command_id
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > 256
            or any(c.isspace() or ord(c) < 0x20 for c in value)
        ):
            raise ValueError("command id is invalid")
        return value

    @staticmethod
    def _validate_turn(dispatch_id: str, generation: int, attempt: int, thread_id: str, turn_id: str) -> None:
        try:
            DispatchId(dispatch_id)
        except ValueError as exc:
            raise ValueError("dispatch id is invalid") from exc
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("generation is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt is invalid")
        ThreadIdentity(thread_id)
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or len(turn_id) > 512
            or any(c.isspace() or ord(c) < 0x20 for c in turn_id)
        ):
            raise ValueError("turn id is invalid")

    def steer(
        self,
        dispatch_id: str,
        *,
        generation: int,
        attempt: int,
        thread_id: str,
        turn_id: str,
        text: str,
        command_id: str | None = None,
    ) -> ControlCommand:
        self._validate_turn(dispatch_id, generation, attempt, thread_id, turn_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192:
            raise ValueError("steer text exceeds its byte limit")
        response = self._send_control(
            "steer",
            dispatch_id=dispatch_id,
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
            command_id=self._validate_command_id(command_id),
            payload=text,
        )
        try:
            return _decode_command(response.get("command"))
        except ControlClientError as exc:
            raise ControlCommandPostSendUncertain(
                "live-control reply was malformed; durable command state is uncertain"
            ) from exc

    def interrupt(
        self,
        dispatch_id: str,
        *,
        generation: int,
        attempt: int,
        thread_id: str,
        turn_id: str,
        command_id: str | None = None,
    ) -> ControlCommand:
        self._validate_turn(dispatch_id, generation, attempt, thread_id, turn_id)
        response = self._send_control(
            "interrupt",
            dispatch_id=dispatch_id,
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
            command_id=self._validate_command_id(command_id),
            payload=None,
        )
        try:
            return _decode_command(response.get("command"))
        except ControlClientError as exc:
            raise ControlCommandPostSendUncertain(
                "live-control reply was malformed; durable command state is uncertain"
            ) from exc

    def command_status(self, command_id: str) -> ControlCommand | None:
        """Read one exact durable command identity without mutation or polling."""

        if not isinstance(command_id, str):
            raise ValueError("command id is invalid")
        response = self._request("control_status", command_id=self._validate_command_id(command_id))
        command = response.get("command")
        return None if command is None else _decode_command(command)

    def _recovery(
        self,
        dispatch_id: str,
        *,
        action_id: str,
        expected_revision: int,
        action_kind: RecoveryActionKind,
        reason: str,
        requested_budget: RetryBudgetChange | None = None,
        compatibility_rebind: CompatibilityRebind | None = None,
    ) -> RecoveryAction:
        try:
            DispatchId(dispatch_id)
        except ValueError as exc:
            raise ValueError("dispatch id is invalid") from exc
        if (
            not isinstance(action_id, str)
            or not action_id.strip()
            or len(action_id.encode("utf-8")) > 256
            or any(c.isspace() or ord(c) < 0x20 for c in action_id)
        ):
            raise ValueError("recovery action id is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("recovery revision is invalid")
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512 or "\x00" in reason:
            raise ValueError("recovery reason is invalid")
        facts: dict[str, object] = {
            "dispatch_id": dispatch_id,
            "action_id": action_id,
            "expected_revision": expected_revision,
            "action_kind": action_kind.value,
            "reason": reason,
        }
        if requested_budget is not None:
            facts["requested_budget"] = requested_budget.to_json()
        if compatibility_rebind is not None:
            facts["compatibility_rebind"] = compatibility_rebind.to_json()
        response = self._request("recovery_action", **facts)
        return _decode_action(response.get("action"))

    def retry(self, dispatch_id: str, *, action_id: str, expected_revision: int, reason: str) -> RecoveryAction:
        return self._recovery(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            action_kind=RecoveryActionKind.RETRY,
            reason=reason,
        )

    def cancel(self, dispatch_id: str, *, action_id: str, expected_revision: int, reason: str) -> RecoveryAction:
        return self._recovery(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            action_kind=RecoveryActionKind.CANCEL,
            reason=reason,
        )

    def change_budget(
        self,
        dispatch_id: str,
        *,
        action_id: str,
        expected_revision: int,
        reason: str,
        requested_budget: RetryBudgetChange,
    ) -> RecoveryAction:
        if not isinstance(requested_budget, RetryBudgetChange):
            raise ValueError("requested budget is not typed")
        return self._recovery(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            action_kind=RecoveryActionKind.BUDGET_CHANGE,
            reason=reason,
            requested_budget=requested_budget,
        )

    def rebind_compatibility(
        self,
        dispatch_id: str,
        *,
        action_id: str,
        expected_revision: int,
        reason: str,
        expected_compatibility_sha256: str,
        proposed_compatibility_sha256: str,
        expected_profile_sha256: str,
        proposed_profile_sha256: str,
        expected_generation: int,
        expected_attempt: int,
    ) -> RecoveryAction:
        rebind = CompatibilityRebind(
            expected_compatibility_sha256=expected_compatibility_sha256,
            proposed_compatibility_sha256=proposed_compatibility_sha256,
            expected_generation=expected_generation,
            expected_attempt=expected_attempt,
            expected_profile_sha256=expected_profile_sha256,
            proposed_profile_sha256=proposed_profile_sha256,
        )
        return self._recovery(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
            reason=reason,
            compatibility_rebind=rebind,
        )


class ControllerDecisionClient:
    """Typed local IPC client shared by model generations and the later TUI."""

    def __init__(self, socket_path: Path, *, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        if timeout <= 0:
            raise ValueError("control client timeout must be positive")
        self.timeout = timeout

    @classmethod
    def for_state_root(cls, state_root: Path, *, timeout: float = 5.0) -> ControllerDecisionClient:
        return cls(Path(state_root).resolve() / ".codex-flow" / "runtime" / "supervisor.sock", timeout=timeout)

    def _request(self, operation: str, **facts: object) -> dict[str, object]:
        try:
            response = send_request(
                self.socket_path, {"version": 1, "operation": operation, **facts}, timeout=self.timeout
            )
        except (IpcError, OSError) as exc:
            raise ControlClientError("supervisor control endpoint is unavailable") from exc
        return _response(response, operation=operation)

    def pending(self) -> tuple[ControllerDecisionStatus, ...]:
        response = self._request("controller_pending")
        decisions = response["decisions"]
        if not isinstance(decisions, list):
            raise ControlClientError("supervisor returned malformed controller decisions")
        return tuple(_decode_controller_status(item) for item in decisions)

    def status(self, decision_id: str) -> ControllerDecisionStatus:
        identity = ControllerDecisionId(decision_id)
        response = self._request("controller_status", decision_id=str(identity))
        return _decode_controller_status(response["decision"])

    def claim(
        self,
        decision_id: str,
        *,
        claimant_id: str,
        expected_revision: int,
        generation: int | None = None,
        claimant_kind: ControllerClaimantKind | str = ControllerClaimantKind.MODEL,
        token: str | None = None,
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(decision_id)
        facts: dict[str, object] = {
            "decision_id": str(identity),
            "claimant_kind": claimant_kind.value
            if isinstance(claimant_kind, ControllerClaimantKind)
            else claimant_kind,
            "claimant_id": claimant_id,
            "expected_revision": expected_revision,
        }
        if generation is not None:
            facts["generation"] = generation
        if token is not None:
            facts["token"] = token
        return _decode_controller_claim(self._request("controller_claim", **facts)["claim"])

    def renew_claim(
        self, decision_id: str, *, claimant_id: str, token: str, expected_revision: int
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(decision_id)
        return _decode_controller_claim(
            self._request(
                "controller_renew_claim",
                decision_id=str(identity),
                claimant_id=claimant_id,
                token=token,
                expected_revision=expected_revision,
            )["claim"]
        )

    def defer_rate_limit(
        self,
        decision_id: str,
        *,
        generation: int,
        claimant_id: str,
        token: str,
        expected_revision: int,
        retry_at: str,
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(decision_id)
        return _decode_controller_claim(
            self._request(
                "controller_defer_rate_limit",
                decision_id=str(identity),
                generation=generation,
                claimant_id=claimant_id,
                token=token,
                expected_revision=expected_revision,
                retry_at=retry_at,
            )["claim"]
        )

    def submit_actions(
        self, bundle: ModelFacingControllerActionBundle, *, claimant_id: str, token: str
    ) -> ControllerActionReceipt:
        if not isinstance(bundle, ModelFacingControllerActionBundle):
            raise ValueError("controller action bundle must be typed")
        response = self._request(
            "controller_submit_actions", bundle=bundle.to_json(), claimant_id=claimant_id, token=token
        )
        return _decode_controller_receipt(response["receipt"])

    def acknowledge(
        self,
        decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerActionReceipt:
        identity = ControllerDecisionId(decision_id)
        response = self._request(
            "controller_acknowledge",
            decision_id=str(identity),
            action_id=action_id,
            bundle_sha256=bundle_sha256,
            committed_revision=committed_revision,
            claimant_id=claimant_id,
            token=token,
        )
        return _decode_controller_receipt(response["receipt"])

    def submit_recovered_actions(
        self, decision_or_bundle: ControllerDecisionId | str | ModelFacingControllerActionBundle
    ) -> ControllerActionReceipt:
        """Commit the exact bundle returned by one reserved SDK inspection.

        Recovery has no access to the source generation's secret claim token;
        the supervisor therefore exposes a separate typed operation whose
        ledger boundary requires the persisted completed-inspection fact.
        """

        if isinstance(decision_or_bundle, ModelFacingControllerActionBundle):
            payload: dict[str, object] = {"bundle": decision_or_bundle.to_json()}
        else:
            payload = {"decision_id": str(ControllerDecisionId(str(decision_or_bundle)))}
        response = self._request("controller_submit_recovered_actions", **payload)
        return _decode_controller_receipt(response["receipt"])

    def acknowledge_recovered(
        self,
        decision_id: str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
    ) -> ControllerActionReceipt:
        """Acknowledge a receipt committed by the reserved recovery reader."""

        identity = ControllerDecisionId(decision_id)
        response = self._request(
            "controller_acknowledge_recovered",
            decision_id=str(identity),
            action_id=action_id,
            bundle_sha256=bundle_sha256,
            committed_revision=committed_revision,
        )
        return _decode_controller_receipt(response["receipt"])

    @staticmethod
    def _decode_generation(value: object) -> ControllerGenerationStatus:
        if not isinstance(value, dict):
            raise ControlClientError("supervisor returned malformed controller generation")
        try:
            return ControllerGenerationStatus(
                ControllerDecisionId(value["decision_id"]),  # type: ignore[arg-type]
                Generation(value["generation"]),  # type: ignore[arg-type]
                value["lineage_id"],  # type: ignore[arg-type]
                value["state"],  # type: ignore[arg-type]
                Generation(value["predecessor_generation"])
                if value.get("predecessor_generation") is not None
                else None,  # type: ignore[arg-type]
                value.get("source_kind", "source_controller"),  # type: ignore[arg-type]
                value.get("prompt_sha256"),  # type: ignore[arg-type]
                ThreadIdentity(value["controller_thread_id"]) if value.get("controller_thread_id") else None,  # type: ignore[arg-type]
                value.get("controller_turn_id"),  # type: ignore[arg-type]
                value.get("inspection_outcome"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ControlClientError("supervisor returned malformed controller generation") from exc

    def reserve_recovery_inspection(self, decision_id: str) -> ControllerRecoveryInspectionClaim:
        identity = ControllerDecisionId(decision_id)
        response = self._request("controller_reserve_recovery", decision_id=str(identity))
        raw = response["claim"]
        if not isinstance(raw, dict):
            raise ControlClientError("supervisor returned malformed controller inspection claim")
        try:
            return ControllerRecoveryInspectionClaim(
                ControllerDecisionId(raw["decision_id"]),
                Generation(raw["generation"]),
                raw["revision"],  # type: ignore[arg-type]
                raw["lease_expires_at"],  # type: ignore[arg-type]
                raw["token"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ControlClientError("supervisor returned malformed controller inspection claim") from exc

    def generation(self, decision_id: str, generation: int | None = None) -> ControllerGenerationStatus:
        """Read one typed persisted generation without exposing SQLite rows."""

        identity = ControllerDecisionId(decision_id)
        facts: dict[str, object] = {"decision_id": str(identity)}
        if generation is not None:
            facts["generation"] = generation
        response = self._request("controller_generation", **facts)
        return self._decode_generation(response["generation"])

    def prepare_generation(
        self, decision_id: str, *, generation: int, prompt_sha256: str | None = None
    ) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        facts: dict[str, object] = {"decision_id": str(identity), "generation": generation}
        if prompt_sha256 is not None:
            facts["prompt_sha256"] = prompt_sha256
        response = self._request("controller_prepare_generation", **facts)
        return self._decode_generation(response["generation"])

    def bind_generation_thread(
        self, decision_id: str, *, generation: int, controller_thread_id: str
    ) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        response = self._request(
            "controller_bind_generation_thread",
            decision_id=str(identity),
            generation=generation,
            controller_thread_id=controller_thread_id,
        )
        return self._decode_generation(response["generation"])

    def bind_generation_turn(
        self,
        decision_id: str,
        *,
        generation: int,
        controller_thread_id: str,
        controller_turn_id: str,
        previous_controller_turn_id: str | None = None,
    ) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        facts: dict[str, object] = {
            "decision_id": str(identity),
            "generation": generation,
            "controller_thread_id": controller_thread_id,
            "controller_turn_id": controller_turn_id,
        }
        if previous_controller_turn_id is not None:
            facts["previous_controller_turn_id"] = previous_controller_turn_id
        response = self._request(
            "controller_bind_generation_turn",
            **facts,
        )
        return self._decode_generation(response["generation"])

    def complete_generation(self, decision_id: str, *, generation: int, state: str) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        response = self._request(
            "controller_complete_generation", decision_id=str(identity), generation=generation, state=state
        )
        return self._decode_generation(response["generation"])

    def reset_generation_delivery(
        self,
        decision_id: str,
        *,
        generation: int,
        expected_revision: int,
        claimant_id: str,
        token: str,
    ) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        response = self._request(
            "controller_reset_generation_delivery",
            decision_id=str(identity),
            generation=generation,
            expected_revision=expected_revision,
            claimant_id=claimant_id,
            token=token,
        )
        return self._decode_generation(response["generation"])

    def complete_recovery_inspection(
        self,
        decision_id: str,
        *,
        inspection_outcome: str,
        claim: ControllerRecoveryInspectionClaim,
        bundle: ModelFacingControllerActionBundle | None = None,
    ) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        if not isinstance(inspection_outcome, str) or not inspection_outcome.strip() or len(inspection_outcome) > 64:
            raise ValueError("controller inspection outcome is invalid")
        response = self._request(
            "controller_complete_recovery",
            decision_id=str(identity),
            inspection_outcome=inspection_outcome,
            claim={
                "decision_id": str(claim.decision_id),
                "generation": int(claim.generation),
                "revision": claim.revision,
                "lease_expires_at": claim.lease_expires_at,
                "token": claim.token,
            },
            **({"bundle": bundle.to_json()} if bundle is not None else {}),
        )
        return self._decode_generation(response["generation"])

    def request_recovery(self, decision_id: str, *, inspection_outcome: str) -> ControllerGenerationStatus:
        identity = ControllerDecisionId(decision_id)
        response = self._request("controller_recover", decision_id=str(identity), inspection_outcome=inspection_outcome)
        return self._decode_generation(response["generation"])


__all__ = [
    "ControlClientError",
    "ControlCommandPostSendUncertain",
    "ControlCommandRejected",
    "ControllerDecisionClient",
    "LiveWorkerControlClient",
]
