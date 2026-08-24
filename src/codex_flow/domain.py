"""Controller-owned types at the stable SDK boundary.

Raw ``openai_codex`` objects deliberately do not cross this module boundary.
The controller can therefore test transport behaviour without starting a
Codex process or importing the SDK in its domain code.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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


class NativePermissionMode(str, Enum):
    INHERIT_NATIVE = "inherit_native"
    READ_ONLY = "read_only"


@dataclass(frozen=True, slots=True)
class NativePermissionAuthority:
    """Effective native authority retained monotonically across SDK processes."""

    mode: NativePermissionMode
    sandbox_mode: str
    approval_policy: str

    def __post_init__(self) -> None:
        if self.sandbox_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("effective native sandbox mode is unsupported")
        if self.approval_policy not in {"untrusted", "on-request", "never"}:
            raise ValueError("effective native approval policy is unsupported")
        if self.mode is NativePermissionMode.READ_ONLY and self.sandbox_mode != "read-only":
            raise ValueError("read-only capsule mode must retain a read-only sandbox")

    @property
    def facts(self) -> JsonObject:
        return {
            "mode": self.mode.value,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "monotonic": True,
        }

    @classmethod
    def from_facts(cls, value: Mapping[str, JsonValue]) -> NativePermissionAuthority:
        if set(value) != {"mode", "sandbox_mode", "approval_policy", "monotonic"}:
            raise ValueError("effective native permission facts have an unsupported shape")
        if value.get("monotonic") is not True:
            raise ValueError("effective native permission facts are not monotonic")
        mode = value.get("mode")
        sandbox = value.get("sandbox_mode")
        approval = value.get("approval_policy")
        if not isinstance(mode, str) or not isinstance(sandbox, str) or not isinstance(approval, str):
            raise ValueError("effective native permission facts have invalid values")
        return cls(NativePermissionMode(mode), sandbox, approval)

    def meet(self, candidate: NativePermissionAuthority) -> NativePermissionAuthority:
        """Return the no-broader authority, rejecting incomparable approvals."""

        sandbox_rank = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
        sandbox = min((self.sandbox_mode, candidate.sandbox_mode), key=sandbox_rank.__getitem__)
        if self.approval_policy == candidate.approval_policy:
            approval = self.approval_policy
        elif "never" in {self.approval_policy, candidate.approval_policy}:
            approval = "never"
        else:
            raise ValueError("native approval policies are incomparable")
        mode = (
            NativePermissionMode.READ_ONLY
            if NativePermissionMode.READ_ONLY in {self.mode, candidate.mode}
            else NativePermissionMode.INHERIT_NATIVE
        )
        return NativePermissionAuthority(mode, sandbox, approval)


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
_LANE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


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


# ---------------------------------------------------------------------------
# H3 execution workspace and controller contracts
# ---------------------------------------------------------------------------


class WorkspaceMode(str, Enum):
    CURRENT_CHECKOUT = "current_checkout"
    EXISTING_WORKTREE = "existing_worktree"
    MANAGED_WORKTREE = "managed_worktree"


class ExecutionStatus(str, Enum):
    PLANNED = "planned"
    THREAD_STARTED = "thread_started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN_PRE_IDENTITY = "uncertain_pre_identity"


class ControllerCheckpoint(str, Enum):
    CAPSULE_PLANNED = "capsule_planned"
    WORKSPACE_LEASED = "workspace_leased"
    WORKSPACE_BASELINE_DURABLE = "workspace_baseline_durable"
    THREAD_STARTING = "thread_starting"
    THREAD_IDENTITY_DURABLE = "thread_identity_durable"
    TURN_DURABLE = "turn_durable"
    VALIDATION_DURABLE = "validation_durable"
    RESULT_DURABLE = "result_durable"


class ValidationFailureCode(str, Enum):
    """Controller-owned, non-sensitive validation failure categories."""

    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    INTEGRITY_FAILURE = "integrity_failure"


@dataclass(frozen=True, slots=True)
class ValidationSpec:
    argv: tuple[str, ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item or "\x00" in item for item in self.argv):
            raise ValueError("validation argv must contain non-empty strings")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("validation timeout must be positive")


_SCHEMA_TYPES = frozenset({"object", "array", "string", "boolean", "number", "integer", "null"})
_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items"})


def validate_output_schema(schema: Mapping[str, object], *, root: bool = True) -> None:
    """Validate the deliberately small recursive schema subset we send to the SDK."""

    if not isinstance(schema, Mapping):
        raise ValueError("output schema nodes must be objects")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ValueError(f"output schema contains unsupported keys: {sorted(map(str, unknown))!r}")
    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES:
        raise ValueError("output schema type must be one of the supported JSON types")
    if root and schema_type != "object":
        raise ValueError("execution output schema must require an object")

    if schema_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError("object schema properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str):
                raise ValueError("object schema property names must be strings")
            validate_output_schema(child, root=False)
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise ValueError("object schema required must be an array")
        if any(not isinstance(name, str) for name in required) or len(set(required)) != len(required):
            raise ValueError("object schema required must contain unique strings")
        if any(name not in properties for name in required):
            raise ValueError("object schema required fields must be declared in properties")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise ValueError("object schema additionalProperties must be a boolean")
    elif schema_type == "array":
        if "items" in schema:
            validate_output_schema(schema["items"], root=False)
        for key in ("properties", "required", "additionalProperties"):
            if key in schema:
                raise ValueError(f"array schema cannot contain {key}")
    else:
        for key in ("properties", "required", "additionalProperties", "items"):
            if key in schema:
                raise ValueError(f"{schema_type} schema cannot contain {key}")


def _relative_paths(values: tuple[str, ...], *, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        path = Path(value)
        if (
            not value
            or value == "."
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != value
            or value in seen
        ):
            raise ValueError(f"{label} must contain unique repository-relative paths")
        seen.add(value)


@dataclass(frozen=True, slots=True)
class ExecutionCapsule:
    capsule_version: int
    run_id: RunId
    milestone_id: MilestoneId
    repository_root: Path
    workspace_mode: WorkspaceMode
    workspace_path: Path
    branch: str
    base_sha: str
    lane: str
    mutable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    validation: ValidationSpec
    model: str
    reasoning_effort: ReasoningEffort
    prompt: str
    output_schema: JsonObject
    permission_mode: NativePermissionMode = NativePermissionMode.INHERIT_NATIVE

    def __post_init__(self) -> None:
        if isinstance(self.capsule_version, bool) or not isinstance(self.capsule_version, int):
            raise ValueError("execution capsule version must be an integer")
        if self.capsule_version != 2:
            raise ValueError("unsupported execution capsule version")
        if not self.repository_root.is_absolute() or not self.workspace_path.is_absolute():
            raise ValueError("repository and workspace paths must be absolute")
        if _LANE_PATTERN.fullmatch(self.lane) is None:
            raise ValueError("workspace lane must be a lowercase semantic slug")
        if not self.branch or any(character.isspace() for character in self.branch):
            raise ValueError("execution branch must be explicit and whitespace-free")
        if self.workspace_mode is WorkspaceMode.MANAGED_WORKTREE and self.branch != f"agent/{self.lane}":
            raise ValueError("managed worktree branch must match agent/<lane>")
        if len(self.base_sha) != 40 or re.fullmatch(r"[0-9a-f]{40}", self.base_sha) is None:
            raise ValueError("base SHA must be a resolved lowercase 40-character Git SHA")
        _relative_paths(self.mutable_paths, label="mutable paths")
        _relative_paths(self.protected_paths, label="protected paths")
        for mutable in map(Path, self.mutable_paths):
            for protected in map(Path, self.protected_paths):
                if mutable == protected or mutable in protected.parents or protected in mutable.parents:
                    raise ValueError("mutable and protected paths must not overlap")
        if not self.model.strip() or not self.prompt.strip():
            raise ValueError("model and prompt must be explicit")
        validate_output_schema(self.output_schema)


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseRecord:
    workspace_path: Path
    repository_root: Path
    mode: WorkspaceMode
    branch: str
    base_sha: str
    lane: str
    owner_run_id: RunId
    created_at: str


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    argv: tuple[str, ...]
    exit_code: int
    stdout_sha256: str
    stderr_sha256: str
    timed_out: bool
    duration_seconds: float
    error_code: ValidationFailureCode | None = None

    def __post_init__(self) -> None:
        if self.exit_code == 0 and self.error_code is not None:
            raise ValueError("successful validation cannot carry a failure code")
        if self.timed_out and self.error_code not in {
            None,
            ValidationFailureCode.TIMEOUT,
            ValidationFailureCode.INTEGRITY_FAILURE,
        }:
            raise ValueError("timed-out validation has an incompatible failure code")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    run_id: RunId
    milestone_id: MilestoneId
    workspace_path: Path
    capsule_path: Path
    capsule_sha256: str
    model: str
    reasoning_effort: ReasoningEffort
    status: ExecutionStatus
    checkpoint: ControllerCheckpoint
    thread_id: ThreadIdentity | None
    turn_id: str | None
    turn_output: JsonObject | None
    result: JsonObject | None
    validation: ValidationObservation | None
    protected_before_sha256: str
    protected_after_sha256: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionIntegrityRecord:
    run_id: RunId
    milestone_id: MilestoneId
    provenance: str
    native_profile_sha256: str | None
    native_compatibility_sha256: str | None
    effective_permission: NativePermissionAuthority | None
    effective_permission_sha256: str | None
    workspace_baseline_head_sha: str | None
    workspace_baseline: tuple[tuple[str, str], ...] | None
    workspace_baseline_sha256: str | None
    turn_started_at: str | None
    git_authority_before_sha256: str | None
    git_authority_after_sha256: str | None
    created_at: str
    updated_at: str
