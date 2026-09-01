"""Immutable presentation models for the human terminal control surface."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .domain import ControllerDecisionStatus, DiagnosticEvent, LiveWorkerStatus

_OPAQUE_NAME = re.compile(r"^(?:model|run|milestone|task|thread)?-?[0-9a-f]{12,}$", re.IGNORECASE)


def _human_name(value: str, *, fallback: str) -> str:
    """Keep semantic names visible while moving generated identities to diagnostics."""

    if _OPAQUE_NAME.fullmatch(value):
        return fallback
    words = value.replace("_", " ").replace("-", " ").strip()
    return words.title() if words else fallback


_ROLE_LABELS = {
    "executor": "Implementer",
    "code-reviewer": "Code reviewer",
    "visual-reviewer": "Visual reviewer",
    "architecture-reviewer": "Architecture reviewer",
    "recovery": "Recovery specialist",
    "controller": "Controller",
}

_STATE_LABELS = {
    "running": "Working",
    "pending": "Waiting to start",
    "queued": "Waiting to start",
    "completed": "Completed",
    "accepted": "Accepted",
    "failed": "Needs attention",
    "human_attention_required": "Needs your attention",
    "interrupted": "Interrupted",
    "awaiting_claim": "Waiting for you",
    "claimed": "Claimed",
}


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _age(value: str | None, *, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "not exposed"
    seconds = max(0, int((now - parsed.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _lease(value: str | None, *, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unclaimed"
    seconds = int((parsed.astimezone(UTC) - now).total_seconds())
    return "expired" if seconds <= 0 else f"{seconds}s remaining"


@dataclass(frozen=True, slots=True)
class ActivityView:
    sequence: int
    kind: str
    occurred_at: str
    text: str

    @classmethod
    def from_event(cls, event: DiagnosticEvent) -> ActivityView:
        return cls(event.sequence, event.kind, event.occurred_at, event.text or "")

    @property
    def speaker_label(self) -> str:
        if self.kind in {"agent_message", "assistant_message"}:
            return "Worker"
        if self.kind in {"user_message", "input_message", "steer_applied"}:
            return "You"
        return "System"

    @property
    def event_label(self) -> str:
        labels = {
            "agent_message": "message",
            "assistant_message": "message",
            "user_message": "message",
            "input_message": "message",
            "turn_started": "turn started",
            "turn_completed": "turn completed",
            "steer_applied": "direction sent",
            "interrupt_requested": "stop requested",
        }
        return labels.get(self.kind, self.kind.replace("_", " "))


@dataclass(frozen=True, slots=True)
class WorkerView:
    dispatch_id: str
    run_id: str
    milestone_id: str
    role: str
    state: str
    generation: int
    attempt: int
    thread_id: str | None
    turn_id: str | None
    model: str
    effort: str
    route: str
    elapsed: str
    activity_age: str
    lease: str
    retry: str
    activity: tuple[ActivityView, ...]

    @property
    def controller_label(self) -> str:
        return _human_name(self.run_id, fallback="Workflow")

    @property
    def task_label(self) -> str:
        return _human_name(self.milestone_id, fallback="Current task")

    @property
    def role_label(self) -> str:
        return _ROLE_LABELS.get(self.role, _human_name(self.role, fallback="Worker"))

    @property
    def state_label(self) -> str:
        return _STATE_LABELS.get(self.state, _human_name(self.state, fallback="Unknown"))

    @classmethod
    def from_status(cls, status: LiveWorkerStatus, *, now: datetime | None = None) -> WorkerView:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        run, milestone, role, _generation = status.dispatch_id.parts
        activity = tuple(ActivityView.from_event(item) for item in status.recent_activity[-12:])
        last_seen = activity[-1].occurred_at if activity else None
        retry = status.retry_policy
        retry_label = (
            f"{retry.strategy.value}; rev {retry.revision}; "
            f"pre {retry.pre_identity_used}/{retry.pre_identity_budget}; "
            f"schema {retry.schema_envelope_used}/{retry.schema_envelope_budget}"
        )
        return cls(
            dispatch_id=str(status.dispatch_id),
            run_id=str(run),
            milestone_id=str(milestone),
            role=str(role),
            state=status.state,
            generation=status.generation,
            attempt=status.attempt,
            thread_id=status.thread_id.id if status.thread_id is not None else None,
            turn_id=status.active_turn_id,
            model="not exposed by control API",
            effort="not exposed by control API",
            route="control: authenticated local supervisor; execution: not exposed",
            elapsed="not exposed by control API",
            activity_age=_age(last_seen, now=observed),
            lease="not exposed by control API",
            retry=retry_label,
            activity=activity,
        )


@dataclass(frozen=True, slots=True)
class DecisionView:
    decision_id: str
    dispatch_id: str
    kind: str
    state: str
    revision: int
    generation: int
    budget: str
    claimant: str
    lease: str
    deadline: str
    summary: str
    source_thread_id: str | None
    expected_successors: tuple[str, ...]

    @property
    def kind_label(self) -> str:
        labels = {
            "checkpoint": "Progress check",
            "human_attention_required": "Help needed",
            "completion": "Completion review",
        }
        return labels.get(self.kind, _human_name(self.kind, fallback="Decision"))

    @property
    def state_label(self) -> str:
        return _STATE_LABELS.get(self.state, _human_name(self.state, fallback="Unknown"))

    @classmethod
    def from_status(cls, status: ControllerDecisionStatus, *, now: datetime | None = None) -> DecisionView:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        claimant = "unclaimed"
        if status.claimant_kind is not None:
            claimant = f"{status.claimant_kind.value}:{status.claimant_id or 'unknown'}"
        return cls(
            decision_id=str(status.decision_id),
            dispatch_id=str(status.dispatch_id),
            kind=status.kind,
            state=status.state.value,
            revision=status.revision,
            generation=int(status.current_generation),
            budget=f"{status.generation_used}/{status.generation_budget}",
            claimant=claimant,
            lease=_lease(status.claim_expires_at, now=observed),
            deadline=status.deadline,
            summary=status.summary.summary,
            source_thread_id=status.summary.source_thread_id,
            expected_successors=status.summary.expected_successor_dispatch_ids,
        )


@dataclass(frozen=True, slots=True)
class TerminalUiSnapshot:
    connected: bool
    observed_at: str
    workers: tuple[WorkerView, ...]
    decisions: tuple[DecisionView, ...]
    connection_error: str | None = None

    @property
    def mode_label(self) -> str:
        return "LIVE · authenticated supervisor" if self.connected else "OFFLINE · read-only last snapshot"


__all__ = ["ActivityView", "DecisionView", "TerminalUiSnapshot", "WorkerView"]
