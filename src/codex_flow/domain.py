"""Controller-owned types at the stable SDK boundary.

Raw ``openai_codex`` objects deliberately do not cross this module boundary.
The controller can therefore test transport behaviour without starting a
Codex process or importing the SDK in its domain code.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
Schema: TypeAlias = Mapping[str, JsonValue]
MAX_JSON_BYTES = 16 * 1_048_576


class StrictJSONError(ValueError):
    """A JSON payload is not an unambiguous RFC-style JSON value."""


class _FrozenList(tuple[object, ...]):
    """Immutable JSON array retaining ordinary list equality semantics."""

    __hash__ = tuple.__hash__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list | tuple):
            return tuple(self) == tuple(other)
        return NotImplemented


def _strict_json_constant(value: str) -> object:
    # ``parse_constant`` is called for all three non-standard tokens.  Do not
    # include the provider token in the exception: provider content must never
    # become durable error text.
    raise StrictJSONError("JSON contains a non-standard numeric constant")


def _strict_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StrictJSONError("JSON contains a non-finite number")
    return result


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _validate_interoperable_json(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise StrictJSONError("JSON contains an unpaired Unicode surrogate")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_interoperable_json(key)
            _validate_interoperable_json(child)
        return
    if isinstance(value, list | tuple):
        for child in value:
            _validate_interoperable_json(child)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictJSONError("JSON contains a non-finite number")


def strict_json_loads(value: str | bytes | bytearray, *, max_bytes: int = MAX_JSON_BYTES) -> object:
    """Decode only unambiguous, finite JSON values.

    The decoder rejects NaN/Infinity, oversized exponents that become
    non-finite, duplicate keys recursively at every object depth, non-UTF-8
    byte encodings, and every Unicode byte-order mark.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("JSON byte limit must be a positive integer")
    if isinstance(value, bytes | bytearray):
        raw = bytes(value)
        if len(raw) > max_bytes:
            raise StrictJSONError("JSON exceeds the byte limit")
        if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            raise StrictJSONError("JSON must be strict UTF-8 without a byte-order mark")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("JSON is not valid strict UTF-8") from exc
    elif isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise StrictJSONError("JSON contains invalid Unicode") from exc
        if len(encoded) > max_bytes:
            raise StrictJSONError("JSON exceeds the byte limit")
        text = value
    else:
        raise StrictJSONError("JSON input must be text or bytes")
    if text.startswith("\ufeff"):
        raise StrictJSONError("JSON must be strict UTF-8 without a byte-order mark")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_strict_json_constant,
            parse_float=_strict_json_float,
        )
        _validate_interoperable_json(decoded)
        return decoded
    except StrictJSONError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise StrictJSONError("JSON is not valid interoperable data") from exc


def freeze_json(value: object) -> object:
    """Detach JSON-compatible mappings/lists into immutable owned values."""

    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        try:
            entries = tuple(value.items())
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("JSON mappings must expose one stable item snapshot") from exc
        for entry in entries:
            if isinstance(entry, str | bytes | bytearray):
                raise ValueError("JSON mappings must expose key/value pairs")
            try:
                pair = tuple(entry)
            except (TypeError, ValueError, RuntimeError) as exc:
                raise ValueError("JSON mappings must expose key/value pairs") from exc
            if len(pair) != 2:
                raise ValueError("JSON mappings must expose key/value pairs")
            key, child = pair
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            if key in frozen:
                raise ValueError("JSON mappings must not contain duplicate keys")
            frozen[key] = freeze_json(child)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        try:
            entries = tuple(value)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError("JSON sequences must expose one stable item snapshot") from exc
        return _FrozenList(freeze_json(child) for child in entries)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON values must contain only finite numbers")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON strings must not contain unpaired Unicode surrogates")
        return value
    if value is None or isinstance(value, int | bool | float):
        return value
    raise ValueError("JSON values must use supported scalar, mapping, and list types")


def thaw_json(value: object) -> object:
    """Return a detached mutable JSON tree suitable for SDK/JSON APIs."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(child) for child in value]
    return value


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


class AcceptanceMode(str, Enum):
    """Independent acceptance authorities a milestone may require."""

    OBJECTIVE = "objective"
    VISUAL = "visual"
    ARCHITECTURE = "architecture"


@dataclass(frozen=True, slots=True)
class RenderedEvidence:
    """Fixed, review-only evidence for a subjective acceptance mode.

    Rendered evidence is intentionally represented by a digest and bounded
    metadata rather than raw pixels.  The producer owns the actual artifact;
    reviewers receive this immutable identity and can never write through the
    H4 controller callback.
    """

    evidence_id: str
    revision: str
    artifact_sha256: str
    artifact_path: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        _required_text(self.evidence_id, label="rendered evidence id", limit=256)
        _required_text(self.revision, label="rendered evidence revision", limit=256)
        _required_text(self.artifact_path, label="rendered evidence path", limit=512)
        evidence_path = Path(self.artifact_path)
        if evidence_path.is_absolute() or ".." in evidence_path.parts or evidence_path.as_posix() != self.artifact_path:
            raise ValueError("rendered evidence path must be repository-relative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256):
            raise ValueError("rendered evidence digest must be a lowercase SHA-256")
        for label, value in (("width", self.width), ("height", self.height)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"rendered evidence {label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class AuthorityAssignment:
    """One concrete role route selected for a required acceptance authority."""

    mode: AcceptanceMode | None
    role: RoleId
    model: str
    reasoning_effort: ReasoningEffort

    def __post_init__(self) -> None:
        if self.mode is not None and not isinstance(self.mode, AcceptanceMode):
            object.__setattr__(self, "mode", AcceptanceMode(self.mode))
        if not isinstance(self.role, RoleId):
            object.__setattr__(self, "role", RoleId(self.role))
        if not isinstance(self.model, str) or not self.model.strip() or "\x00" in self.model:
            raise ValueError("authority model must be a bounded non-empty string")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            object.__setattr__(self, "reasoning_effort", ReasoningEffort(self.reasoning_effort))


@dataclass(frozen=True, slots=True)
class AuthorityPlan:
    """The immutable routing decision for one H4 multi-authority run."""

    assignments: tuple[AuthorityAssignment, ...]

    def __post_init__(self) -> None:
        assignments = tuple(self.assignments)
        roles = [str(item.role) for item in assignments]
        if len(roles) != len(set(roles)):
            raise ValueError("required H4 authorities must use distinct roles")
        modes = [item.mode for item in assignments if item.mode is not None]
        if len(modes) != len(set(modes)):
            raise ValueError("required H4 acceptance modes must use distinct authorities")
        object.__setattr__(self, "assignments", assignments)

    def for_mode(self, mode: AcceptanceMode | str) -> AuthorityAssignment:
        target = mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode)
        for assignment in self.assignments:
            if assignment.mode is target:
                return assignment
        raise KeyError(target)

    def role(self, role: RoleId | str) -> AuthorityAssignment:
        target = str(role)
        for assignment in self.assignments:
            if str(assignment.role) == target:
                return assignment
        raise KeyError(target)


class FindingCausalClass(str, Enum):
    """Causal classes used to diagnose a review finding without attempt counts."""

    IMPLEMENTATION = "implementation"
    CONTRACT = "contract"
    ACCEPTANCE = "acceptance"
    SECURITY = "security"
    ENVIRONMENT = "environment"
    TRANSPORT = "transport"
    REVIEW = "review"
    STALE_REVIEW = "stale_review"


class Severity(str, Enum):
    """Severity is intentionally independent from successor promotion impact."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# Short aliases keep the public contract readable for callers that use the
# plan's prose names rather than the longer enum class names.
CausalClass = FindingCausalClass
FindingSeverity = Severity


class RecoveryOutcome(str, Enum):
    """Typed recovery outcomes; terminal and non-terminal meanings are distinct."""

    FINISH_LOCALLY = "finish_locally"
    CHANGE_STRATEGY = "change_strategy"
    CONTINUE_WITH_REPLAN = "continue_with_replan"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"


class LifecyclePhase(str, Enum):
    """Durable H4 walking-skeleton phases."""

    EXECUTION = "execution"
    REVIEW = "review"
    REPAIR = "repair"
    RECOVERY = "recovery"
    DECISION = "decision"
    BUDGET = "budget"
    ACCEPTANCE = "acceptance"
    ROUTING = "routing"


class BudgetExhaustion(str, Enum):
    """Which bounded resource stopped another external turn."""

    TURNS = "turns"
    REPAIRS = "repairs"
    REVIEWS = "reviews"
    COMPACTIONS = "compactions"
    VALIDATIONS = "validations"
    WALL_CLOCK = "wall_clock"


class LifecycleStatus(str, Enum):
    """Observable result labels kept distinct from recovery decisions."""

    ACCEPTED = "accepted"
    CONTINUE_WITH_REPLAN = "continue_with_replan"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"
    CONTEXT_ROLLOVER = "context_rollover"
    TRANSPORT_FAILURE = "transport_failure"
    STALE_REVIEW = "stale_review"
    LIMIT_EXHAUSTED = "limit_exhausted"
    REJECTED_AFTER_REPAIR = "rejected_after_repair"
    STALE_AUTHORITY = "stale_authority"
    ROUTE_UNAVAILABLE = "route_unavailable"
    NOTIFICATION_FAILURE = "notification_failure"


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


def _required_text(value: str, *, label: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _owned_json_object(value: Mapping[str, object], *, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        frozen = freeze_json(dict(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must contain interoperable JSON") from exc
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise ValueError(f"{label} must be a JSON object")
    return cast(JsonObject, frozen)


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """A stable, independently reviewable finding."""

    finding_id: str
    causal_class: FindingCausalClass
    severity: Severity
    promotion_blocking: bool
    promotion_reason: str
    evidence: JsonObject
    criterion: str
    defer_to: str | None = None
    survives_prior_repair: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or _ID_PATTERN.fullmatch(self.finding_id) is None:
            raise ValueError("finding id must be a stable bounded identifier")
        if not isinstance(self.causal_class, FindingCausalClass):
            raise ValueError("finding causal class must be typed")
        if not isinstance(self.severity, Severity):
            raise ValueError("finding severity must be typed")
        if not isinstance(self.promotion_blocking, bool):
            raise ValueError("promotion_blocking must be boolean")
        _required_text(self.promotion_reason, label="promotion reason")
        _required_text(self.criterion, label="acceptance criterion")
        if self.defer_to is not None:
            _required_text(self.defer_to, label="defer_to", limit=256)
        object.__setattr__(self, "evidence", _owned_json_object(self.evidence, label="finding evidence"))

    @property
    def blocks_successor(self) -> bool:
        return self.promotion_blocking

    @property
    def id(self) -> str:
        return self.finding_id

    @property
    def reason(self) -> str:
        return self.promotion_reason

    @property
    def promotion_impact(self) -> bool:
        return self.promotion_blocking


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Read-only reviewer output whose freshness is explicit and durable."""

    review_id: str
    reviewer_role: RoleId
    accepted: bool
    findings: tuple[ReviewFinding, ...]
    reviewed_revision: str
    fresh: bool = True
    read_only: bool = True
    prior_review_id: str | None = None
    acceptance_mode: AcceptanceMode = AcceptanceMode.OBJECTIVE
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.review_id, label="review id", limit=256)
        _required_text(self.reviewed_revision, label="reviewed revision", limit=256)
        if not isinstance(self.reviewer_role, RoleId):
            object.__setattr__(self, "reviewer_role", RoleId(self.reviewer_role))
        findings = tuple(self.findings)
        if len({finding.finding_id for finding in findings}) != len(findings):
            raise ValueError("review findings must have unique stable identities")
        if not self.read_only:
            raise ValueError("review result must declare read-only authority")
        if self.accepted and any(finding.promotion_blocking for finding in findings):
            raise ValueError("accepted review cannot contain promotion-blocking findings")
        if not isinstance(self.acceptance_mode, AcceptanceMode):
            object.__setattr__(self, "acceptance_mode", AcceptanceMode(self.acceptance_mode))
        evidence_ids = tuple(self.evidence_ids)
        if any(not isinstance(value, str) or not value.strip() for value in evidence_ids):
            raise ValueError("review evidence ids must be non-empty strings")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "findings", findings)

    @property
    def promotion_blockers(self) -> tuple[ReviewFinding, ...]:
        return tuple(finding for finding in self.findings if finding.promotion_blocking)


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Durable repair acknowledgement, normally owned by the original executor."""

    repair_id: str
    finding_id: str
    owner_dispatch_id: DispatchId
    action: str
    outcome: str
    same_owner: bool = True
    prior_finding_survives: bool = False

    def __post_init__(self) -> None:
        _required_text(self.repair_id, label="repair id", limit=256)
        if not isinstance(self.finding_id, str) or not self.finding_id:
            raise ValueError("repair finding id must be non-empty")
        _required_text(self.action, label="repair action")
        _required_text(self.outcome, label="repair outcome")
        if not isinstance(self.owner_dispatch_id, DispatchId):
            object.__setattr__(self, "owner_dispatch_id", DispatchId(self.owner_dispatch_id))
        if self.same_owner is not True:
            raise ValueError("H4-A repairs must remain with the durable owner")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """A typed diagnosis; only the three terminal outcomes are user-visible."""

    outcome: RecoveryOutcome
    rationale: str
    checkpoint: str
    finding_ids: tuple[str, ...] = ()
    external_prerequisite: str | None = None
    decision_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RecoveryOutcome):
            object.__setattr__(self, "outcome", RecoveryOutcome(self.outcome))
        _required_text(self.rationale, label="recovery rationale")
        _required_text(self.checkpoint, label="recovery checkpoint", limit=256)
        if self.outcome is RecoveryOutcome.EXTERNAL_BLOCKED and not self.external_prerequisite:
            raise ValueError("external-blocked recovery requires an actionable prerequisite")
        if self.outcome is RecoveryOutcome.NEEDS_DECISION and not self.decision_request_id:
            raise ValueError("needs-decision recovery requires a durable decision request")
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))


@dataclass(frozen=True, slots=True)
class ReplanProposal:
    """Bounded architecture recovery proposal.

    The controller compares the declared boundary flags before it permits the
    executor to continue.  A proposal is therefore an implementation strategy
    change, never an implicit change to user intent or public authority.
    """

    strategy: str
    intent_unchanged: bool = True
    contract_unchanged: bool = True
    security_boundary_unchanged: bool = True
    cost_unchanged: bool = True
    destructive_behavior_unchanged: bool = True
    scope_unchanged: bool = True

    def __post_init__(self) -> None:
        _required_text(self.strategy, label="revised strategy")
        flags = (
            self.intent_unchanged,
            self.contract_unchanged,
            self.security_boundary_unchanged,
            self.cost_unchanged,
            self.destructive_behavior_unchanged,
            self.scope_unchanged,
        )
        if any(not isinstance(flag, bool) for flag in flags):
            raise ValueError("replan boundary flags must be boolean")

    @property
    def boundary_unchanged(self) -> bool:
        return all(
            (
                self.intent_unchanged,
                self.contract_unchanged,
                self.security_boundary_unchanged,
                self.cost_unchanged,
                self.destructive_behavior_unchanged,
                self.scope_unchanged,
            )
        )


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Durable user decision request; notification is never authoritative."""

    request_id: str
    question: str
    options: tuple[str, ...]
    blocking: bool = True

    def __post_init__(self) -> None:
        _required_text(self.request_id, label="decision request id", limit=256)
        _required_text(self.question, label="decision question")
        options = tuple(_required_text(option, label="decision option", limit=512) for option in self.options)
        if not options or len(set(options)) != len(options):
            raise ValueError("decision request options must be unique and non-empty")
        object.__setattr__(self, "options", options)


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """A validated response to a previously persisted decision request."""

    request_id: str
    choice: str
    rationale: str

    def __post_init__(self) -> None:
        _required_text(self.request_id, label="decision request id", limit=256)
        _required_text(self.choice, label="decision choice", limit=512)
        _required_text(self.rationale, label="decision rationale")


@dataclass(frozen=True, slots=True)
class Budget:
    """Bounded resource usage with fail-closed checks before external turns."""

    max_turns: int = 1
    max_repairs: int = 1
    max_reviews: int = 2
    max_compactions: int = 1
    max_validations: int = 2
    wall_clock_seconds: float = 900.0
    turns: int = 0
    repairs: int = 0
    reviews: int = 0
    compactions: int = 0
    validations: int = 0
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        for name in ("max_turns", "max_repairs", "max_reviews", "max_compactions", "max_validations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.wall_clock_seconds) or self.wall_clock_seconds <= 0:
            raise ValueError("wall_clock_seconds must be positive")
        for name in ("turns", "repairs", "reviews", "compactions", "validations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")

    def exhausted(self) -> BudgetExhaustion | None:
        checks = (
            (self.turns, self.max_turns, BudgetExhaustion.TURNS),
            (self.repairs, self.max_repairs, BudgetExhaustion.REPAIRS),
            (self.reviews, self.max_reviews, BudgetExhaustion.REVIEWS),
            (self.compactions, self.max_compactions, BudgetExhaustion.COMPACTIONS),
            (self.validations, self.max_validations, BudgetExhaustion.VALIDATIONS),
        )
        for used, limit, kind in checks:
            if used >= limit:
                return kind
        if self.elapsed_seconds >= self.wall_clock_seconds:
            return BudgetExhaustion.WALL_CLOCK
        return None


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """One durable H4 fact projected from authoritative ledger events."""

    phase: LifecyclePhase
    kind: str
    sequence: int
    data: JsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.phase, LifecyclePhase):
            object.__setattr__(self, "phase", LifecyclePhase(self.phase))
        _required_text(self.kind, label="lifecycle kind", limit=128)
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("lifecycle sequence must be a positive integer")
        object.__setattr__(self, "data", _owned_json_object(self.data, label="lifecycle data"))


@dataclass(frozen=True, slots=True)
class H4LifecycleResult:
    """Observable terminal walking-skeleton result."""

    status: LifecycleStatus | str
    accepted: bool
    review_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    repair_ids: tuple[str, ...]
    lifecycle: tuple[LifecycleRecord, ...]
    recovery: RecoveryDecision | None = None
    authority_plan: AuthorityPlan | None = None
    rendered_evidence: tuple[RenderedEvidence, ...] = ()
    notification_failure: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, LifecycleStatus):
            try:
                object.__setattr__(self, "status", LifecycleStatus(self.status))
            except ValueError as exc:
                raise ValueError("unknown H4 lifecycle status") from exc
        object.__setattr__(self, "review_ids", tuple(self.review_ids))
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))
        object.__setattr__(self, "repair_ids", tuple(self.repair_ids))
        object.__setattr__(self, "lifecycle", tuple(self.lifecycle))
        object.__setattr__(self, "rendered_evidence", tuple(self.rendered_evidence))
        if not isinstance(self.notification_failure, bool):
            raise ValueError("notification_failure must be boolean")


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
MAX_OUTPUT_SCHEMA_DEPTH = 32
MAX_OUTPUT_SCHEMA_PROPERTIES = 1_024
MAX_OUTPUT_OBJECT_PROPERTIES = 128
MAX_OUTPUT_PROPERTY_NAME_BYTES = 256
MAX_OUTPUT_ARRAY_ITEMS = 1_024
MAX_STRUCTURED_OUTPUT_BYTES = 1_048_576


def validate_output_schema(schema: Mapping[str, object]) -> None:
    """Validate the one closed recursive schema contract accepted at every boundary."""

    properties_seen = 0

    def validate_node(node: object, *, depth: int, root: bool) -> None:
        nonlocal properties_seen
        if not isinstance(node, Mapping):
            raise ValueError("output schema nodes must be objects")
        if depth > MAX_OUTPUT_SCHEMA_DEPTH:
            raise ValueError("output schema exceeds the depth limit")
        unknown = set(node) - _SCHEMA_KEYS
        if unknown:
            raise ValueError(f"output schema contains unsupported keys: {sorted(map(str, unknown))!r}")
        schema_type = node.get("type")
        if not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES:
            raise ValueError("output schema type must be one of the supported JSON types")
        if root and schema_type != "object":
            raise ValueError("execution output schema must require an object")

        if schema_type == "object":
            if set(node) != {"type", "properties", "required", "additionalProperties"}:
                raise ValueError("object schemas must declare properties, required, and additionalProperties")
            properties = node["properties"]
            required = node["required"]
            if not isinstance(properties, Mapping):
                raise ValueError("object schema properties must be an object")
            if len(properties) > MAX_OUTPUT_OBJECT_PROPERTIES:
                raise ValueError("object schema exceeds the per-object property limit")
            properties_seen += len(properties)
            if properties_seen > MAX_OUTPUT_SCHEMA_PROPERTIES:
                raise ValueError("output schema exceeds the total property limit")
            if not isinstance(required, list | tuple):
                raise ValueError("object schema required must be an array")
            if any(not isinstance(name, str) for name in required) or len(set(required)) != len(required):
                raise ValueError("object schema required must contain unique strings")
            if set(required) != set(properties):
                raise ValueError("object schema must require every declared property exactly once")
            if node["additionalProperties"] is not False:
                raise ValueError("object schema additionalProperties must be explicitly false")
            for name, child in properties.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name.encode("utf-8")) > MAX_OUTPUT_PROPERTY_NAME_BYTES
                    or "\x00" in name
                ):
                    raise ValueError("object schema property names must be bounded non-empty strings")
                validate_node(child, depth=depth + 1, root=False)
            return

        if schema_type == "array":
            if set(node) != {"type", "items"}:
                raise ValueError("array schemas must contain exactly type and an explicit item schema")
            validate_node(node["items"], depth=depth + 1, root=False)
            return

        if set(node) != {"type"}:
            raise ValueError(f"{schema_type} schemas may contain only type")

    try:
        validate_node(schema, depth=1, root=True)
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError("output schema is not a valid JSON schema mapping") from exc


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
    acceptance_modes: tuple[AcceptanceMode, ...] = (AcceptanceMode.OBJECTIVE,)

    def __post_init__(self) -> None:
        if isinstance(self.capsule_version, bool) or not isinstance(self.capsule_version, int):
            raise ValueError("execution capsule version must be an integer")
        if self.capsule_version != 2:
            raise ValueError("unsupported execution capsule version")
        try:
            repository_root = self.repository_root.resolve(strict=False)
            workspace_path = self.workspace_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("repository and workspace paths must have a canonical physical identity") from exc
        object.__setattr__(self, "repository_root", repository_root)
        object.__setattr__(self, "workspace_path", workspace_path)
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
        try:
            # A frozen dataclass alone is not sufficient here: callers can
            # retain and mutate nested dict/list aliases after construction.
            # Own the entire schema tree before it can reach a digest, SDK,
            # ledger, or artifact.
            canonical_schema = freeze_json(self.output_schema)
        except (TypeError, RecursionError) as exc:
            raise ValueError("execution output schema is not a valid JSON object") from exc
        if not isinstance(canonical_schema, Mapping):  # pragma: no cover - predicate already enforces object root
            raise ValueError("execution output schema must be an object")
        validate_output_schema(canonical_schema)
        object.__setattr__(self, "output_schema", canonical_schema)
        modes = tuple(
            mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in self.acceptance_modes
        )
        if not modes or len(modes) != len(set(modes)):
            raise ValueError("execution capsule acceptance modes must be non-empty and unique")
        object.__setattr__(self, "acceptance_modes", modes)


def canonicalize_execution_capsule(capsule: ExecutionCapsule) -> ExecutionCapsule:
    """Revalidate and detach a capsule at every public durability boundary."""

    if not isinstance(capsule, ExecutionCapsule):
        raise ValueError("execution plan requires an ExecutionCapsule")
    # Reconstruct every field rather than trusting frozen-instance internals:
    # a caller with access to ``object.__setattr__`` or a custom mapping must
    # not bypass the public validation boundary before any SQLite write.
    return ExecutionCapsule(
        capsule.capsule_version,
        capsule.run_id,
        capsule.milestone_id,
        capsule.repository_root,
        capsule.workspace_mode,
        capsule.workspace_path,
        capsule.branch,
        capsule.base_sha,
        capsule.lane,
        tuple(capsule.mutable_paths),
        tuple(capsule.protected_paths),
        capsule.validation,
        capsule.model,
        capsule.reasoning_effort,
        capsule.prompt,
        capsule.output_schema,
        capsule.permission_mode,
        tuple(capsule.acceptance_modes),
    )


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
    workspace_terminal_head_sha: str | None
    workspace_terminal: tuple[tuple[str, str], ...] | None
    workspace_terminal_sha256: str | None
    turn_started_at: str | None
    git_authority_before_sha256: str | None
    git_authority_after_sha256: str | None
    created_at: str
    updated_at: str
