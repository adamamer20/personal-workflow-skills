"""Controller-owned types at the stable SDK boundary.

Raw ``openai_codex`` objects deliberately do not cross this module boundary.
The controller can therefore test transport behaviour without starting a
Codex process or importing the SDK in its domain code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
Schema: TypeAlias = Mapping[str, JsonValue]


class Sandbox(str, Enum):
    """Filesystem policy accepted by the SDK adapter."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class ReasoningEffort(str, Enum):
    """Explicit reasoning effort; no implicit SDK default is accepted."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class Capability(str, Enum):
    """Capabilities recorded by the H1 compatibility sentinel."""

    LOCAL_START = "local_start"
    THREAD_IDENTITY = "thread_identity"
    SCHEMA_BOUNDED_TURN = "schema_bounded_turn"
    EXPLICIT_MODEL = "explicit_model"
    EXPLICIT_REASONING_EFFORT = "explicit_reasoning_effort"
    LIFECYCLE_EVENTS = "lifecycle_events"
    SAME_THREAD_RESUME = "same_thread_resume"
    SANDBOX_ISOLATION = "sandbox_isolation"
    REVIEW = "review"
    STRUCTURED_SKILL_INPUT = "structured_skill_input"
    DESKTOP = "desktop"
    IDLE_WAKE = "idle_wake"
    REMOTE_HOST = "remote_host"
    PERMISSION_PROFILE = "permission_profile"


class CapabilityStatus(str, Enum):
    """Truthful compatibility labels; status is never inferred from presence."""

    PROVEN = "proven"
    UNSUPPORTED = "unsupported"
    NOT_EXPOSED = "not_exposed"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    capability: Capability
    status: CapabilityStatus
    detail: str


@dataclass(frozen=True, slots=True)
class ThreadIdentity:
    """Addressable SDK thread identity owned by the controller."""

    id: str

    def __post_init__(self) -> None:
        if not self.id or any(character.isspace() for character in self.id):
            raise ValueError("thread identity must be a non-empty, whitespace-free string")


@dataclass(frozen=True, slots=True)
class SkillInput:
    """Structured skill reference accepted by the SDK input surface."""

    name: str
    path: str

    def __post_init__(self) -> None:
        if not self.name or not self.path:
            raise ValueError("skill input requires a name and path")


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    sequence: int
    method: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnObservation:
    thread_id: ThreadIdentity
    turn_id: str
    status: str
    final_response: str | None
    structured_output: JsonObject | None
    events: tuple[LifecycleEvent, ...]
    error: str | None = None


class CodexFlowError(RuntimeError):
    """Base error for deterministic SDK-boundary failures."""


class TransportFailureBeforeIdentity(CodexFlowError):
    """The SDK failed before a durable thread identity was returned."""


class TerminalFailureAfterIdentity(CodexFlowError):
    """The SDK reached a thread identity and then failed terminally."""


class UnsupportedCapability(CodexFlowError):
    """A requested optional SDK capability is not exposed by the installed SDK."""


# ---------------------------------------------------------------------------
# H2 durable workflow contracts
# ---------------------------------------------------------------------------


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class _ValidatedIdentifier(str):
    """A short, path-safe identifier that can safely cross typed boundaries."""

    label = "identifier"

    def __new__(cls, value: str) -> _ValidatedIdentifier:
        if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{cls.label} must match {_ID_PATTERN.pattern!r}")
        return str.__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


class RunId(_ValidatedIdentifier):
    label = "run id"


class MilestoneId(_ValidatedIdentifier):
    label = "milestone id"


class RoleId(_ValidatedIdentifier):
    label = "role"


class DispatchId(str):
    """Logical dispatch identity: ``run/milestone/role/generation``."""

    label = "dispatch id"

    def __new__(cls, value: str) -> DispatchId:
        if not isinstance(value, str):
            raise ValueError(f"{cls.label} must be a string")
        parts = value.split("/")
        if len(parts) != 4:
            raise ValueError("dispatch id must be <run>/<milestone>/<role>/<generation>")
        RunId(parts[0])
        MilestoneId(parts[1])
        RoleId(parts[2])
        Generation(parts[3])
        return str.__new__(cls, value)

    @classmethod
    def from_parts(
        cls,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        role: RoleId | str,
        generation: Generation | int | str,
    ) -> DispatchId:
        run = RunId(run_id)
        milestone = MilestoneId(milestone_id)
        role_value = RoleId(role)
        generation_value = Generation(generation)
        return cls(f"{run}/{milestone}/{role_value}/{generation_value}")

    @property
    def value(self) -> str:
        return str(self)

    @property
    def parts(self) -> tuple[RunId, MilestoneId, RoleId, Generation]:
        run, milestone, role, generation = str(self).split("/")
        return RunId(run), MilestoneId(milestone), RoleId(role), Generation(generation)


class EventId(str):
    """Stable event identity, scoped to a milestone sequence."""

    def __new__(cls, value: str) -> EventId:
        if not isinstance(value, str) or len(value) > 256 or not value.strip():
            raise ValueError("event id must be a non-empty bounded string")
        if any(character.isspace() for character in value):
            raise ValueError("event id must not contain whitespace")
        return str.__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


class Generation(int):
    """Positive dispatch generation; generations never silently increment."""

    def __new__(cls, value: int | str) -> Generation:
        if isinstance(value, bool):
            raise ValueError("generation must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("generation must be a positive integer") from exc
        if str(parsed) != str(value) and not isinstance(value, int):
            raise ValueError("generation must be a canonical integer")
        if parsed < 1:
            raise ValueError("generation must be a positive integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class EventSequence(int):
    """Positive monotonically increasing sequence within one milestone."""

    def __new__(cls, value: int | str) -> EventSequence:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("event sequence must be a positive integer") from exc
        if isinstance(value, bool) or parsed < 1:
            raise ValueError("event sequence must be a positive integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class SchemaVersion(int):
    """Version marker for the SQLite schema."""

    def __new__(cls, value: int | str) -> SchemaVersion:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("schema version must be a non-negative integer") from exc
        if isinstance(value, bool) or parsed < 0:
            raise ValueError("schema version must be a non-negative integer")
        return int.__new__(cls, parsed)

    @property
    def value(self) -> int:
        return int(self)


class WorkflowState(str, Enum):
    PLANNED = "PLANNED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    NEEDS_DECISION = "NEEDS_DECISION"
    COMPLETED = "COMPLETED"
    REVIEWING = "REVIEWING"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.ACCEPTED, WorkflowState.BLOCKED, WorkflowState.FAILED, WorkflowState.CANCELLED}
)


def coerce_state(value: WorkflowState | str) -> WorkflowState:
    if isinstance(value, WorkflowState):
        return value
    try:
        return WorkflowState(value)
    except ValueError as exc:
        raise ValueError(f"unknown workflow state: {value!r}") from exc


def is_transition_allowed(from_state: WorkflowState | str, to_state: WorkflowState | str) -> bool:
    """Return the single canonical H2 transition-policy decision."""

    source = coerce_state(from_state)
    target = coerce_state(to_state)
    match source:
        case WorkflowState.PLANNED:
            return target in {
                WorkflowState.STARTING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.STARTING | WorkflowState.NEEDS_DECISION | WorkflowState.REPAIR_REQUIRED:
            return target in {
                WorkflowState.RUNNING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.RUNNING:
            return target in {
                WorkflowState.NEEDS_DECISION,
                WorkflowState.COMPLETED,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.COMPLETED:
            return target in {
                WorkflowState.REVIEWING,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.REVIEWING:
            return target in {
                WorkflowState.REPAIR_REQUIRED,
                WorkflowState.ACCEPTED,
                WorkflowState.BLOCKED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        case WorkflowState.ACCEPTED | WorkflowState.BLOCKED | WorkflowState.FAILED | WorkflowState.CANCELLED:
            return False
    raise AssertionError(f"unhandled workflow state: {source!r}")


ALLOWED_TRANSITIONS: Mapping[WorkflowState, frozenset[WorkflowState]] = MappingProxyType(
    {
        source: frozenset(target for target in WorkflowState if is_transition_allowed(source, target))
        for source in WorkflowState
    }
)


class ReasonCode(str, Enum):
    """Stable reason categories owned by the controller."""

    DECISION_REQUIRED = "decision_required"
    ACCEPTANCE_AMBIGUITY = "acceptance_ambiguity"
    CONTRACT_CHANGE = "contract_change"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    REVIEW_REJECTED = "review_rejected"
    CONTEXT_ROLLOVER = "context_rollover"
    TRANSPORT_FAILURE = "transport_failure"
    EXECUTION_FAILURE = "execution_failure"
    TERMINAL_OUTCOME = "terminal_outcome"
    DISPATCH_CLAIMED = "dispatch_claimed"


@dataclass(frozen=True, slots=True)
class WorkflowReason:
    code: ReasonCode
    detail: None = None

    def __post_init__(self) -> None:
        if self.detail is not None:
            raise ValueError("durable workflow reasons do not carry free-form detail")


class PreIdentityTransportFailure(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.TRANSPORT_FAILURE)


class PostIdentityExecutionFailure(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.EXECUTION_FAILURE)


class ReviewRejected(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.REVIEW_REJECTED)


class TerminalOutcome(WorkflowReason):
    def __init__(self) -> None:
        super().__init__(ReasonCode.TERMINAL_OUTCOME)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: RunId
    created_at: str
    closed_at: str | None
    metadata: JsonObject


@dataclass(frozen=True, slots=True)
class MilestoneRecord:
    run_id: RunId
    milestone_id: MilestoneId
    state: WorkflowState
    created_at: str
    updated_at: str
    metadata: JsonObject


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    dispatch_id: DispatchId
    run_id: RunId
    milestone_id: MilestoneId
    role: RoleId
    generation: Generation
    claimed_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: EventId
    run_id: RunId
    milestone_id: MilestoneId
    sequence: EventSequence
    from_state: WorkflowState | None
    to_state: WorkflowState
    event_type: str
    reason: WorkflowReason | None
    occurred_at: str
    dispatch_id: DispatchId | None = None
    data: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class RecoveryFact:
    dispatch: DispatchClaim
    state: WorkflowState
    last_event: EventRecord


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    run: RunRecord
    milestones: tuple[MilestoneRecord, ...]
    dispatches: tuple[DispatchClaim, ...]
    events: tuple[EventRecord, ...]
