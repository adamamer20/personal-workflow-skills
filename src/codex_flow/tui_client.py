"""Async event-driven façade over the frozen local control clients."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contracts import ModelFacingControllerAction, ModelFacingControllerActionBundle
from .control_client import (
    ControlClientError,
    ControlCommandPostSendUncertain,
    ControlCommandRejected,
    ControllerDecisionClient,
    LiveWorkerControlClient,
)
from .domain import (
    ControlCommand,
    ControlCommandKind,
    ControllerActionKind,
    ControllerClaimantKind,
    ControllerDecisionClaim,
    ControllerDecisionStatus,
    ConversationHistoryPage,
    ConversationHistoryStatus,
    LiveWorkerStatus,
    redact_control_text,
)
from .tui_models import DecisionView, TerminalUiSnapshot, WorkerView


class TerminalUiOfflineError(RuntimeError):
    """A mutation was requested without a live authenticated supervisor."""


_RECONCILIATION_UNAVAILABLE = object()


@dataclass(frozen=True, slots=True)
class TerminalUiCommandResult:
    operation: str
    identity: str
    detail: str
    outcome: str = "accepted"

    def __post_init__(self) -> None:
        if self.outcome not in {"accepted", "rejected", "post_send_uncertain"}:
            raise ValueError("terminal command presentation outcome is invalid")


@dataclass(slots=True)
class _PendingControlRequest:
    command_id: str
    dispatch_id: str
    generation: int
    attempt: int
    thread_id: str
    turn_id: str
    kind: ControlCommandKind
    payload: str | None
    payload_sha256: str
    resend_used: bool = False
    uncertain: bool = False


class TerminalUiClient:
    """One explicit-refresh client; it never owns a timer or background poll."""

    def __init__(self, live: LiveWorkerControlClient, decisions: ControllerDecisionClient) -> None:
        self._live = live
        self._decisions = decisions
        self._last_snapshot = TerminalUiSnapshot(False, datetime.now(UTC).isoformat(), (), (), "not connected")
        self._claims: dict[str, ControllerDecisionClaim] = {}
        self._claimant_id = f"terminal-ui-{uuid.uuid4().hex}"
        self._worker_statuses: dict[str, LiveWorkerStatus] = {}
        self._decision_statuses: dict[str, ControllerDecisionStatus] = {}
        self._pending_controls: dict[tuple[object, ...], _PendingControlRequest] = {}
        self._history_pages: dict[str, list[ConversationHistoryPage]] = {}
        self._history_bindings: dict[str, tuple[str, int | None, int | None, int | None]] = {}

    @classmethod
    def for_state_root(cls, state_root: Path) -> TerminalUiClient:
        root = Path(state_root)
        return cls(LiveWorkerControlClient.for_state_root(root), ControllerDecisionClient.for_state_root(root))

    @property
    def snapshot(self) -> TerminalUiSnapshot:
        return self._last_snapshot

    async def refresh(self) -> TerminalUiSnapshot:
        """Refresh only when called by a user or lifecycle event."""

        observed = datetime.now(UTC)
        try:
            workers, decisions = await asyncio.gather(
                asyncio.to_thread(self._live.status),
                asyncio.to_thread(self._decisions.pending),
            )
        except (ControlClientError, OSError) as exc:
            self._history_pages.clear()
            self._history_bindings.clear()
            self._last_snapshot = TerminalUiSnapshot(
                False,
                observed.isoformat(),
                self._last_snapshot.workers,
                self._last_snapshot.decisions,
                str(exc),
            )
            return self._last_snapshot
        worker_views = tuple(WorkerView.from_status(item, now=observed) for item in workers)
        decision_views = tuple(DecisionView.from_status(item, now=observed) for item in decisions)
        current_bindings = {
            str(item.dispatch_id): (
                item.thread_id.id if item.thread_id is not None else "",
                int(item.generation),
                item.attempt,
                None,
            )
            for item in workers
        }
        current_bindings.update(
            {
                str(item.decision_id): (
                    item.summary.source_thread_id or "",
                    int(item.current_generation),
                    None,
                    item.revision,
                )
                for item in decisions
            }
        )
        for subject_id, binding in tuple(self._history_bindings.items()):
            if current_bindings.get(subject_id) != binding:
                self._history_pages.pop(subject_id, None)
                self._history_bindings.pop(subject_id, None)
        self._last_snapshot = TerminalUiSnapshot(
            True,
            observed.isoformat(),
            worker_views,
            decision_views,
        )
        self._worker_statuses = {str(item.dispatch_id): item for item in workers}
        self._decision_statuses = {str(item.decision_id): item for item in decisions}
        return self._last_snapshot

    async def load_conversation(
        self, *, worker_id: str | None = None, decision_id: str | None = None, older: bool = False
    ) -> ConversationHistoryPage:
        """Load one explicit history page and retain only ephemeral pages."""

        if (worker_id is None) == (decision_id is None):
            raise ValueError("conversation load requires exactly one worker or decision")
        if worker_id is not None:
            status = self.worker_status(worker_id)
            subject_kind, subject_id = "worker", worker_id
            thread_id = status.thread_id.id if status.thread_id is not None else None
            generation, attempt, revision = int(status.generation), status.attempt, None
        else:
            assert decision_id is not None
            status = self.decision_status(decision_id)
            subject_kind, subject_id = "controller", decision_id
            thread_id = status.summary.source_thread_id
            generation, attempt, revision = int(status.current_generation), None, status.revision
        if thread_id is None:
            raise ValueError("selected conversation has no thread identity")
        reader = getattr(self._live, "conversation_history", None)
        if not callable(reader):
            raise ValueError("conversation history is not exposed by the control client")
        pages = self._history_pages.setdefault(subject_id, [])
        token = pages[0].older_token if older and pages else None
        page = await asyncio.to_thread(
            reader,
            subject_kind=subject_kind,
            subject_id=subject_id,
            thread_id=thread_id,
            generation=generation,
            attempt=attempt,
            revision=revision,
            page_token=token,
        )
        if page.status is ConversationHistoryStatus.STALE:
            self._history_pages.pop(subject_id, None)
            self._history_bindings.pop(subject_id, None)
        elif page.status is ConversationHistoryStatus.AVAILABLE:
            if not older:
                self._history_pages[subject_id] = [page]
            elif pages and page.snapshot_token == pages[0].snapshot_token:
                self._history_pages[subject_id] = [page, *pages]
            else:
                self._history_pages[subject_id] = [page]
        else:
            self._history_pages[subject_id] = [page]
        if page.status is not ConversationHistoryStatus.STALE:
            self._history_bindings[subject_id] = (thread_id, generation, attempt, revision)
        return page

    def conversation_pages(self, subject_id: str) -> tuple[ConversationHistoryPage, ...]:
        return tuple(self._history_pages.get(subject_id, ()))

    def worker_status(self, dispatch_id: str) -> LiveWorkerStatus:
        try:
            return self._worker_statuses[dispatch_id]
        except KeyError as exc:
            raise ValueError("selected worker is not in the current typed snapshot") from exc

    def decision_status(self, decision_id: str) -> ControllerDecisionStatus:
        try:
            return self._decision_statuses[decision_id]
        except KeyError as exc:
            raise ValueError("selected decision is not in the current typed snapshot") from exc

    def _require_live(self) -> None:
        if not self._last_snapshot.connected:
            raise TerminalUiOfflineError("offline snapshot is read-only; reconnect before mutation")

    def open_resume_thread_id(self, *, worker_id: str | None, decision_id: str | None) -> str:
        """Return one current inactive thread identity or fail closed."""

        self._require_live()
        if (worker_id is None) == (decision_id is None):
            raise ValueError("resume requires exactly one selected worker or decision")
        if worker_id is not None:
            status = self.worker_status(worker_id)
            if status.thread_id is None or status.active_turn_id is not None:
                raise ValueError("selected worker lacks a current typed inactive-turn proof")
            return status.thread_id.id
        assert decision_id is not None
        status = self.decision_status(decision_id)
        visible = tuple(item for item in self._last_snapshot.decisions if item.decision_id == decision_id)
        if len(visible) != 1 or visible[0].revision != status.revision:
            raise ValueError("selected decision revision is not current")
        source_thread_id = status.summary.source_thread_id
        if source_thread_id is None:
            raise ValueError("selected decision has no source thread identity")
        matches = tuple(
            worker
            for worker in self._worker_statuses.values()
            if worker.thread_id is not None and worker.thread_id.id == source_thread_id
        )
        if len(matches) != 1 or matches[0].active_turn_id is not None:
            raise ValueError("decision source thread lacks one current typed inactive-turn proof")
        return source_thread_id

    @staticmethod
    def _control_key(status: LiveWorkerStatus, kind: ControlCommandKind, payload: str | None) -> tuple[object, ...]:
        assert status.thread_id is not None and status.active_turn_id is not None
        normalized = None if payload is None else redact_control_text(payload)
        digest = hashlib.sha256((normalized or "").encode("utf-8")).hexdigest()
        return (
            str(status.dispatch_id),
            int(status.generation),
            status.attempt,
            status.thread_id.id,
            status.active_turn_id,
            kind.value,
            digest,
        )

    @staticmethod
    def _matches_request(command: ControlCommand, request: _PendingControlRequest) -> bool:
        return (
            command.command_id == request.command_id
            and str(command.dispatch_id) == request.dispatch_id
            and int(command.generation) == request.generation
            and command.attempt == request.attempt
            and command.thread_id.id == request.thread_id
            and command.turn_id == request.turn_id
            and command.kind is request.kind
            and command.payload_sha256 == request.payload_sha256
        )

    @staticmethod
    def _command_result(operation: str, command: ControlCommand) -> TerminalUiCommandResult:
        if command.state.value == "rejected":
            outcome = "rejected"
        elif command.state.value == "unresolved":
            outcome = "post_send_uncertain"
        else:
            outcome = "accepted"
        return TerminalUiCommandResult(
            operation,
            command.command_id,
            f"canonical {command.state.value} for {command.turn_id}",
            outcome,
        )

    async def _send_control(
        self, status: LiveWorkerStatus, kind: ControlCommandKind, payload: str | None
    ) -> TerminalUiCommandResult:
        self._require_live()
        if status.thread_id is None or status.active_turn_id is None:
            raise ValueError("selected worker has no active SDK turn")
        key = self._control_key(status, kind, payload)
        request = self._pending_controls.get(key)
        if request is None:
            request = _PendingControlRequest(
                uuid.uuid4().hex,
                str(status.dispatch_id),
                int(status.generation),
                status.attempt,
                status.thread_id.id,
                status.active_turn_id,
                kind,
                payload,
                str(key[-1]),
            )
            self._pending_controls[key] = request
        elif (
            request.dispatch_id,
            request.generation,
            request.attempt,
            request.thread_id,
            request.turn_id,
            request.kind.value,
            request.payload_sha256,
        ) != key:
            raise RuntimeError("retained control command identity conflicts with the semantic request")

        operation = kind.value

        def send() -> ControlCommand:
            facts = {
                "generation": request.generation,
                "attempt": request.attempt,
                "thread_id": request.thread_id,
                "turn_id": request.turn_id,
                "command_id": request.command_id,
            }
            if kind is ControlCommandKind.STEER:
                return self._live.steer(request.dispatch_id, text=request.payload, **facts)  # type: ignore[arg-type]
            return self._live.interrupt(request.dispatch_id, **facts)

        async def reconcile() -> ControlCommand | object | None:
            try:
                current = await asyncio.to_thread(self._live.command_status, request.command_id)
            except (ControlClientError, OSError):
                return _RECONCILIATION_UNAVAILABLE
            if current is not None and not self._matches_request(current, request):
                raise RuntimeError("durable control command does not match the retained semantic identity")
            return current

        if request.uncertain:
            command = await reconcile()
            if command is _RECONCILIATION_UNAVAILABLE:
                return TerminalUiCommandResult(
                    operation,
                    request.command_id,
                    "durable command status is unavailable",
                    "post_send_uncertain",
                )
            if command is not None:
                assert isinstance(command, ControlCommand)
                result = self._command_result(operation, command)
                if result.outcome != "post_send_uncertain":
                    self._pending_controls.pop(key, None)
                return result
            if request.resend_used:
                return TerminalUiCommandResult(
                    operation,
                    request.command_id,
                    "durable command status remains absent or unavailable after the one same-id resend",
                    "post_send_uncertain",
                )
            request.resend_used = True

        try:
            command = await asyncio.to_thread(send)
        except ControlCommandRejected as exc:
            self._pending_controls.pop(key, None)
            return TerminalUiCommandResult(operation, request.command_id, str(exc), "rejected")
        except ControlCommandPostSendUncertain:
            request.uncertain = True
            command = await reconcile()
            if command is _RECONCILIATION_UNAVAILABLE:
                return TerminalUiCommandResult(
                    operation,
                    request.command_id,
                    "durable command status could not be reconciled",
                    "post_send_uncertain",
                )
            if command is None and request.resend_used:
                return TerminalUiCommandResult(
                    operation,
                    request.command_id,
                    "same-id resend reply and durable status were not observed",
                    "post_send_uncertain",
                )
            if command is None:
                request.resend_used = True
                try:
                    command = await asyncio.to_thread(send)
                except ControlCommandRejected as exc:
                    self._pending_controls.pop(key, None)
                    return TerminalUiCommandResult(operation, request.command_id, str(exc), "rejected")
                except (ControlCommandPostSendUncertain, ControlClientError, OSError):
                    return TerminalUiCommandResult(
                        operation,
                        request.command_id,
                        "same-id resend reply was not observed",
                        "post_send_uncertain",
                    )
            assert isinstance(command, ControlCommand)
        if not self._matches_request(command, request):
            raise RuntimeError("returned control command does not match the retained semantic identity")
        result = self._command_result(operation, command)
        request.uncertain = result.outcome == "post_send_uncertain"
        if result.outcome != "post_send_uncertain":
            self._pending_controls.pop(key, None)
        return result

    async def steer(self, status: LiveWorkerStatus, text: str) -> TerminalUiCommandResult:
        return await self._send_control(status, ControlCommandKind.STEER, text)

    async def interrupt(self, status: LiveWorkerStatus) -> TerminalUiCommandResult:
        return await self._send_control(status, ControlCommandKind.INTERRUPT, None)

    async def human_claim(
        self, status: ControllerDecisionStatus, *, claimant_id: str | None = None
    ) -> TerminalUiCommandResult:
        self._require_live()
        identity = self._claimant_id if claimant_id is None else claimant_id
        claim = await asyncio.to_thread(
            self._decisions.claim,
            str(status.decision_id),
            claimant_id=identity,
            expected_revision=status.revision,
            generation=int(status.current_generation),
            claimant_kind=ControllerClaimantKind.HUMAN,
        )
        self._claims[str(status.decision_id)] = claim
        return TerminalUiCommandResult("human_claim", str(claim.decision_id), f"lease {claim.lease_expires_at}")

    def _bundle(
        self, status: ControllerDecisionStatus, action: ModelFacingControllerAction, *, rationale: str
    ) -> tuple[ModelFacingControllerActionBundle, ControllerDecisionClaim]:
        claim = self._claims.get(str(status.decision_id))
        if claim is None or claim.token is None:
            raise ValueError("claim this exact decision before submitting an action")
        if claim.revision != status.revision or claim.generation != status.current_generation:
            raise ValueError("visible decision revision no longer matches the human claim")
        action_id = f"tui-{uuid.uuid4().hex}"
        bundle = ModelFacingControllerActionBundle(
            1,
            status.decision_id,
            claim.generation,
            action_id,
            claim.revision,
            status.summary.expected_successor_dispatch_ids,
            (action,),
            rationale,
        )
        return bundle, claim

    async def _submit_and_acknowledge(
        self, status: ControllerDecisionStatus, action: ModelFacingControllerAction, *, rationale: str
    ) -> TerminalUiCommandResult:
        self._require_live()
        bundle, claim = self._bundle(status, action, rationale=rationale)
        receipt = await asyncio.to_thread(
            self._decisions.submit_actions,
            bundle,
            claimant_id=claim.claimant_id,
            token=str(claim.token),
        )
        acknowledged = await asyncio.to_thread(
            self._decisions.acknowledge,
            str(status.decision_id),
            action_id=receipt.action_id,
            bundle_sha256=receipt.bundle_sha256,
            committed_revision=receipt.expected_revision + 1,
            claimant_id=claim.claimant_id,
            token=str(claim.token),
        )
        self._claims.pop(str(status.decision_id), None)
        return TerminalUiCommandResult("decision_action", acknowledged.action_id, acknowledged.state)

    async def acknowledge(self, status: ControllerDecisionStatus) -> TerminalUiCommandResult:
        return await self._submit_and_acknowledge(
            status,
            ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),
            rationale="Human terminal acknowledgement",
        )

    async def rearm_checkpoint(
        self, status: ControllerDecisionStatus, *, checkpoint_seconds: int = 1800
    ) -> TerminalUiCommandResult:
        return await self._submit_and_acknowledge(
            status,
            ModelFacingControllerAction(ControllerActionKind.REARM_CHECKPOINT, checkpoint_seconds=checkpoint_seconds),
            rationale="Human terminal checkpoint re-arm",
        )


__all__ = ["TerminalUiClient", "TerminalUiCommandResult", "TerminalUiOfflineError"]
