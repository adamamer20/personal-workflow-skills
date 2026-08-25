"""Typed model-facing contracts for the workflow-control skill.

The controller persists JSON/JSONL projections, but model authors work with
these small immutable Python records.  Keeping the authoring boundary typed
prevents prompt prose or ad-hoc dictionaries from becoming workflow state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .domain import AcceptanceMode, JsonObject, RoleId

_MAX_TEXT = 16_384
_MAX_ITEMS = 128


def _text(value: str, *, label: str, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _items(values: tuple[str, ...] | list[str], *, label: str) -> tuple[str, ...]:
    normalized = tuple(values)
    if len(normalized) > _MAX_ITEMS or any(not isinstance(item, str) or not item.strip() for item in normalized):
        raise ValueError(f"{label} must contain bounded non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must contain unique values")
    return normalized


class ModelResultStatus(str, Enum):
    """Terminal labels a model may report to the controller boundary."""

    COMPLETED = "completed"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModelAuthority:
    """One distinct acceptance authority declared by a planning model."""

    mode: AcceptanceMode
    role: RoleId

    def __post_init__(self) -> None:
        if not isinstance(self.mode, AcceptanceMode):
            object.__setattr__(self, "mode", AcceptanceMode(self.mode))
        if not isinstance(self.role, RoleId):
            object.__setattr__(self, "role", RoleId(self.role))


@dataclass(frozen=True, slots=True)
class ModelFacingCapsule:
    """The compact typed capsule written by ``plan-work``.

    Runtime routing, workspace identity, and persistence details are selected
    by the controller.  The model supplies intent, acceptance, ownership, and
    a bounded execution prompt only.
    """

    schema_version: int
    objective: str
    decomposition: tuple[str, ...]
    acceptance_modes: tuple[AcceptanceMode, ...]
    acceptance_criteria: tuple[str, ...]
    mutable_surfaces: tuple[str, ...]
    protected_surfaces: tuple[str, ...]
    authorities: tuple[ModelAuthority, ...]
    prompt: str
    recovery_policy: str = "completion_biased"
    prompt_budget_bytes: int = 12_000

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported model-facing capsule schema version")
        _text(self.objective, label="capsule objective")
        _text(self.prompt, label="capsule prompt")
        _text(self.recovery_policy, label="capsule recovery policy", limit=128)
        if self.recovery_policy != "completion_biased":
            raise ValueError("capsule recovery policy must be completion_biased")
        decomposition = _items(self.decomposition, label="capsule decomposition")
        criteria = _items(self.acceptance_criteria, label="capsule acceptance criteria")
        mutable = _items(self.mutable_surfaces, label="capsule mutable surfaces")
        protected = _items(self.protected_surfaces, label="capsule protected surfaces")
        if set(mutable) & set(protected):
            raise ValueError("capsule mutable and protected surfaces must be disjoint")
        modes = tuple(
            mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in self.acceptance_modes
        )
        if not modes or len(modes) != len(set(modes)):
            raise ValueError("capsule acceptance modes must be non-empty and unique")
        authorities = tuple(self.authorities)
        if any(not isinstance(item, ModelAuthority) for item in authorities):
            raise ValueError("capsule authorities must use ModelAuthority records")
        if len(authorities) != len(modes) or {item.mode for item in authorities} != set(modes):
            raise ValueError("capsule authorities must cover each acceptance mode exactly once")
        if len({item.role for item in authorities}) != len(authorities):
            raise ValueError("capsule authorities must be distinct")
        if isinstance(self.prompt_budget_bytes, bool) or not isinstance(self.prompt_budget_bytes, int):
            raise ValueError("capsule prompt budget must be an integer")
        if self.prompt_budget_bytes <= 0 or len(self.prompt.encode("utf-8")) > self.prompt_budget_bytes:
            raise ValueError("capsule prompt exceeds its measured byte budget")
        object.__setattr__(self, "decomposition", decomposition)
        object.__setattr__(self, "acceptance_modes", modes)
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "mutable_surfaces", mutable)
        object.__setattr__(self, "protected_surfaces", protected)
        object.__setattr__(self, "authorities", authorities)

    def to_json(self) -> JsonObject:
        """Return the controller's detached JSON projection."""

        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "decomposition": list(self.decomposition),
            "acceptance_modes": [mode.value for mode in self.acceptance_modes],
            "acceptance_criteria": list(self.acceptance_criteria),
            "mutable_surfaces": list(self.mutable_surfaces),
            "protected_surfaces": list(self.protected_surfaces),
            "authorities": [{"mode": item.mode.value, "role": str(item.role)} for item in self.authorities],
            "prompt": self.prompt,
            "recovery_policy": self.recovery_policy,
            "prompt_budget_bytes": self.prompt_budget_bytes,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ModelFacingCapsule:
        """Parse exactly one model-facing capsule shape; no defaults are guessed."""

        expected = {
            "schema_version",
            "objective",
            "decomposition",
            "acceptance_modes",
            "acceptance_criteria",
            "mutable_surfaces",
            "protected_surfaces",
            "authorities",
            "prompt",
            "recovery_policy",
            "prompt_budget_bytes",
        }
        if set(value) != expected:
            raise ValueError(f"model-facing capsule keys must be exactly {sorted(expected)!r}")

        def strings(key: str) -> tuple[str, ...]:
            raw = value[key]
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ValueError(f"capsule {key} must be an array of strings")
            return tuple(raw)

        raw_authorities = value["authorities"]
        if not isinstance(raw_authorities, list):
            raise ValueError("capsule authorities must be an array")
        authorities: list[ModelAuthority] = []
        for raw in raw_authorities:
            if not isinstance(raw, Mapping) or set(raw) != {"mode", "role"}:
                raise ValueError("capsule authority shape is invalid")
            mode, role = raw["mode"], raw["role"]
            if not isinstance(mode, str) or not isinstance(role, str):
                raise ValueError("capsule authority values must be strings")
            authorities.append(ModelAuthority(AcceptanceMode(mode), RoleId(role)))
        version, objective, prompt, recovery = (
            value["schema_version"],
            value["objective"],
            value["prompt"],
            value["recovery_policy"],
        )
        budget = value["prompt_budget_bytes"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("capsule schema_version must be an integer")
        if not isinstance(objective, str) or not isinstance(prompt, str) or not isinstance(recovery, str):
            raise ValueError("capsule text fields must be strings")
        if isinstance(budget, bool) or not isinstance(budget, int):
            raise ValueError("capsule prompt_budget_bytes must be an integer")
        modes = strings("acceptance_modes")
        return cls(
            version,
            objective,
            strings("decomposition"),
            tuple(AcceptanceMode(mode) for mode in modes),
            strings("acceptance_criteria"),
            strings("mutable_surfaces"),
            strings("protected_surfaces"),
            tuple(authorities),
            prompt,
            recovery,
            budget,
        )


@dataclass(frozen=True, slots=True)
class ModelValidation:
    """A bounded validation fact retained in a model-facing result."""

    name: str
    passed: bool
    evidence: str

    def __post_init__(self) -> None:
        _text(self.name, label="validation name", limit=256)
        if not isinstance(self.passed, bool):
            raise ValueError("validation passed must be boolean")
        _text(self.evidence, label="validation evidence", limit=512)


@dataclass(frozen=True, slots=True)
class ModelFacingResult:
    """The one durable, model-facing result emitted by an executor."""

    schema_version: int
    status: ModelResultStatus
    summary: str
    changed_surfaces: tuple[str, ...]
    validations: tuple[ModelValidation, ...]
    durable_status: str
    next_action: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported model-facing result schema version")
        if not isinstance(self.status, ModelResultStatus):
            object.__setattr__(self, "status", ModelResultStatus(self.status))
        _text(self.summary, label="result summary")
        _text(self.durable_status, label="result durable status", limit=128)
        if self.next_action is not None:
            _text(self.next_action, label="result next action")
        object.__setattr__(self, "changed_surfaces", _items(self.changed_surfaces, label="result changed surfaces"))
        validations = tuple(self.validations)
        if any(not isinstance(item, ModelValidation) for item in validations):
            raise ValueError("result validations must use ModelValidation records")
        object.__setattr__(self, "validations", validations)

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "summary": self.summary,
            "changed_surfaces": list(self.changed_surfaces),
            "validations": [
                {"name": item.name, "passed": item.passed, "evidence": item.evidence} for item in self.validations
            ],
            "durable_status": self.durable_status,
            "next_action": self.next_action,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ModelFacingResult:
        expected = {
            "schema_version",
            "status",
            "summary",
            "changed_surfaces",
            "validations",
            "durable_status",
            "next_action",
        }
        if set(value) != expected:
            raise ValueError(f"model-facing result keys must be exactly {sorted(expected)!r}")
        raw_validations = value["validations"]
        if not isinstance(raw_validations, list):
            raise ValueError("result validations must be an array")
        validations: list[ModelValidation] = []
        for raw in raw_validations:
            if not isinstance(raw, Mapping) or set(raw) != {"name", "passed", "evidence"}:
                raise ValueError("result validation shape is invalid")
            name, passed, evidence = raw["name"], raw["passed"], raw["evidence"]
            if not isinstance(name, str) or not isinstance(passed, bool) or not isinstance(evidence, str):
                raise ValueError("result validation values are invalid")
            validations.append(ModelValidation(name, passed, evidence))
        version = value["schema_version"]
        status = value["status"]
        summary = value["summary"]
        durable_status = value["durable_status"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("result schema_version must be an integer")
        if not isinstance(status, str) or not isinstance(summary, str) or not isinstance(durable_status, str):
            raise ValueError("result scalar fields are invalid")
        changed = value["changed_surfaces"]
        if not isinstance(changed, list) or any(not isinstance(item, str) for item in changed):
            raise ValueError("result changed_surfaces must be an array of strings")
        next_action = value["next_action"]
        if next_action is not None and not isinstance(next_action, str):
            raise ValueError("result next_action must be a string or null")
        return cls(
            version,
            ModelResultStatus(status),
            summary,
            tuple(changed),
            tuple(validations),
            durable_status,
            next_action,
        )


def model_facing_capsule_schema() -> JsonObject:
    """Return the closed JSON schema used when serializing a capsule."""

    text_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "objective": {"type": "string"},
            "decomposition": text_array,
            "acceptance_modes": text_array,
            "acceptance_criteria": text_array,
            "mutable_surfaces": text_array,
            "protected_surfaces": text_array,
            "authorities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}, "role": {"type": "string"}},
                    "required": ["mode", "role"],
                    "additionalProperties": False,
                },
            },
            "prompt": {"type": "string"},
            "recovery_policy": {"type": "string"},
            "prompt_budget_bytes": {"type": "integer"},
        },
        "required": [
            "schema_version",
            "objective",
            "decomposition",
            "acceptance_modes",
            "acceptance_criteria",
            "mutable_surfaces",
            "protected_surfaces",
            "authorities",
            "prompt",
            "recovery_policy",
            "prompt_budget_bytes",
        ],
        "additionalProperties": False,
    }


def model_facing_result_schema() -> JsonObject:
    """Return the closed JSON schema used when serializing a result."""

    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "status": {"type": "string"},
            "summary": {"type": "string"},
            "changed_surfaces": {"type": "array", "items": {"type": "string"}},
            "validations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["name", "passed", "evidence"],
                    "additionalProperties": False,
                },
            },
            "durable_status": {"type": "string"},
            "next_action": {"type": "string"},
        },
        "required": [
            "schema_version",
            "status",
            "summary",
            "changed_surfaces",
            "validations",
            "durable_status",
            "next_action",
        ],
        "additionalProperties": False,
    }


ModelCapsule = ModelFacingCapsule
ModelResult = ModelFacingResult


__all__ = [
    "ModelAuthority",
    "ModelCapsule",
    "ModelFacingCapsule",
    "ModelFacingResult",
    "ModelResult",
    "ModelResultStatus",
    "ModelValidation",
    "model_facing_capsule_schema",
    "model_facing_result_schema",
]
