"""Typed model-facing contracts for the workflow-control skill.

The controller persists JSON/JSONL projections, but model authors work with
these small immutable Python records.  Keeping the authoring boundary typed
prevents prompt prose or ad-hoc dictionaries from becoming workflow state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from .domain import (
    MAX_LOCAL_IMAGE_COUNT,
    AcceptanceMode,
    ControllerActionKind,
    ControllerDecisionId,
    DispatchId,
    Generation,
    JsonObject,
    LocalImageInput,
    RetryBudgetChange,
    RoleId,
    strict_json_loads,
)

_MAX_TEXT = 16_384
_MAX_ITEMS = 128
_MAX_AGENT_MESSAGE_BYTES = 65_536
_TEXT_PATTERN = r"^(?![\s\S]*\u0000)(?=[\s\S]*\S)"
_CANONICAL_ACCEPTANCE_ROLES = {
    AcceptanceMode.OBJECTIVE: RoleId("code-reviewer"),
    AcceptanceMode.VISUAL: RoleId("visual-reviewer"),
    AcceptanceMode.ARCHITECTURE: RoleId("architecture-reviewer"),
}
_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}(?:@[a-z0-9][a-z0-9._-]{0,127})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, *, label: str, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _items(values: tuple[str, ...] | list[str], *, label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple | list):
        raise ValueError(f"{label} must be an array of strings")
    normalized = tuple(values)
    if len(normalized) > _MAX_ITEMS:
        raise ValueError(f"{label} must contain bounded non-empty strings")
    for item in normalized:
        _text(item, label=label)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must contain unique values")
    return normalized


class ModelResultStatus(str, Enum):
    """Terminal labels a model may report to the controller boundary."""

    COMPLETED = "completed"
    NEEDS_DECISION = "needs_decision"
    EXTERNAL_BLOCKED = "external_blocked"
    FAILED = "failed"


class PluginReadiness(str, Enum):
    """Sanitized readiness labels for an installed Codex plugin."""

    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PluginRequirement:
    """One explicit plugin capability required by a model-facing capsule.

    Requirements are immutable identity facts.  They never contain auth
    material, environment values, or an instruction to install or enable a
    plugin.  ``bundle_digest`` binds the exact installed bytes expected by the
    controller at enqueue and again immediately before SDK start.
    """

    canonical_id: str
    version: str
    source: str
    bundle_digest: str
    bundled_skill_ids: tuple[str, ...] = ()
    mcp_connectors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_id, str) or _PLUGIN_ID_PATTERN.fullmatch(self.canonical_id) is None:
            raise ValueError("plugin canonical id must be a lowercase semantic identifier")
        _text(self.version, label="plugin version", limit=128)
        _text(self.source, label="plugin source", limit=256)
        if _SHA256_PATTERN.fullmatch(self.bundle_digest) is None:
            raise ValueError("plugin bundle digest must be a lowercase SHA-256")
        object.__setattr__(self, "bundled_skill_ids", _items(self.bundled_skill_ids, label="bundled skill ids"))
        object.__setattr__(self, "mcp_connectors", _items(self.mcp_connectors, label="MCP connector ids"))

    @property
    def plugin_id(self) -> str:
        """Compatibility spelling for callers that use ``plugin_id``."""

        return self.canonical_id

    @property
    def bundle_sha256(self) -> str:
        return self.bundle_digest

    @property
    def bundled_skills(self) -> tuple[str, ...]:
        return self.bundled_skill_ids

    @property
    def mcp_connector_ids(self) -> tuple[str, ...]:
        return self.mcp_connectors

    def to_json(self) -> JsonObject:
        return {
            "canonical_id": self.canonical_id,
            "version": self.version,
            "source": self.source,
            "bundle_digest": self.bundle_digest,
            "bundled_skill_ids": list(self.bundled_skill_ids),
            "mcp_connectors": list(self.mcp_connectors),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> PluginRequirement:
        expected = {
            "canonical_id",
            "version",
            "source",
            "bundle_digest",
            "bundled_skill_ids",
            "mcp_connectors",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("plugin requirement keys are unsupported")
        skills = value["bundled_skill_ids"]
        connectors = value["mcp_connectors"]
        if (
            not isinstance(skills, list)
            or any(not isinstance(item, str) for item in skills)
            or not isinstance(connectors, list)
            or any(not isinstance(item, str) for item in connectors)
        ):
            raise ValueError("plugin requirement collections are malformed")
        scalar = {name: value[name] for name in ("canonical_id", "version", "source", "bundle_digest")}
        if any(not isinstance(item, str) for item in scalar.values()):
            raise ValueError("plugin requirement scalar fields are malformed")
        return cls(
            scalar["canonical_id"],
            scalar["version"],
            scalar["source"],
            scalar["bundle_digest"],
            tuple(skills),
            tuple(connectors),
        )


@dataclass(frozen=True, slots=True)
class PluginCapabilitySnapshot:
    """A sanitized immutable snapshot of one installed plugin capability."""

    canonical_id: str
    version: str
    source: str
    enabled: bool
    bundle_digest: str
    bundled_skill_ids: tuple[str, ...] = ()
    mcp_connectors: tuple[str, ...] = ()
    readiness: PluginReadiness = PluginReadiness.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_id, str) or _PLUGIN_ID_PATTERN.fullmatch(self.canonical_id) is None:
            raise ValueError("plugin canonical id must be a lowercase semantic identifier")
        _text(self.version, label="plugin version", limit=128)
        _text(self.source, label="plugin source", limit=256)
        if not isinstance(self.enabled, bool):
            raise ValueError("plugin enabled state must be boolean")
        if _SHA256_PATTERN.fullmatch(self.bundle_digest) is None:
            raise ValueError("plugin bundle digest must be a lowercase SHA-256")
        object.__setattr__(self, "bundled_skill_ids", _items(self.bundled_skill_ids, label="bundled skill ids"))
        object.__setattr__(self, "mcp_connectors", _items(self.mcp_connectors, label="MCP connector ids"))
        if not isinstance(self.readiness, PluginReadiness):
            object.__setattr__(self, "readiness", PluginReadiness(self.readiness))

    @property
    def plugin_id(self) -> str:
        return self.canonical_id

    @property
    def bundle_sha256(self) -> str:
        return self.bundle_digest

    @property
    def bundled_skills(self) -> tuple[str, ...]:
        return self.bundled_skill_ids

    @property
    def mcp_connector_ids(self) -> tuple[str, ...]:
        return self.mcp_connectors

    @property
    def capability_digest(self) -> str:
        payload = json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def sha256(self) -> str:
        return self.capability_digest

    def to_json(self) -> JsonObject:
        return {
            "canonical_id": self.canonical_id,
            "version": self.version,
            "source": self.source,
            "enabled": self.enabled,
            "bundle_digest": self.bundle_digest,
            "bundled_skill_ids": list(self.bundled_skill_ids),
            "mcp_connectors": list(self.mcp_connectors),
            "readiness": self.readiness.value,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> PluginCapabilitySnapshot:
        expected = {
            "canonical_id",
            "version",
            "source",
            "enabled",
            "bundle_digest",
            "bundled_skill_ids",
            "mcp_connectors",
            "readiness",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("plugin capability snapshot keys are unsupported")
        skills = value["bundled_skill_ids"]
        connectors = value["mcp_connectors"]
        if (
            not isinstance(skills, list)
            or any(not isinstance(item, str) for item in skills)
            or not isinstance(connectors, list)
            or any(not isinstance(item, str) for item in connectors)
        ):
            raise ValueError("plugin capability collections are malformed")
        scalar = {name: value[name] for name in ("canonical_id", "version", "source", "bundle_digest", "readiness")}
        if any(not isinstance(item, str) for item in scalar.values()):
            raise ValueError("plugin capability scalar fields are malformed")
        if not isinstance(value["enabled"], bool):
            raise ValueError("plugin capability enabled state is malformed")
        return cls(
            scalar["canonical_id"],
            scalar["version"],
            scalar["source"],
            value["enabled"],
            scalar["bundle_digest"],
            tuple(skills),
            tuple(connectors),
            scalar["readiness"],
        )

    def satisfies(self, requirement: PluginRequirement) -> bool:
        return (
            self.canonical_id == requirement.canonical_id
            and self.version == requirement.version
            and self.source == requirement.source
            and self.enabled
            and self.bundle_digest == requirement.bundle_digest
            and set(requirement.bundled_skill_ids).issubset(self.bundled_skill_ids)
            and set(requirement.mcp_connectors).issubset(self.mcp_connectors)
            and self.readiness is PluginReadiness.READY
        )


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
    prompt_budget_bytes: int = field(default=12_000, compare=False)
    plugin_requirements: tuple[PluginRequirement, ...] = field(default=(), compare=True)
    local_image_paths: tuple[str, ...] = field(default=(), compare=True)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in {1, 2, 3}
        ):
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
        if len(mutable) + len(protected) > _MAX_ITEMS:
            raise ValueError("capsule surfaces must contain at most 128 entries")
        if set(mutable) & set(protected):
            raise ValueError("capsule mutable and protected surfaces must be disjoint")
        supplied_modes = tuple(
            mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in self.acceptance_modes
        )
        if not supplied_modes or len(supplied_modes) != len(set(supplied_modes)):
            raise ValueError("capsule acceptance modes must be non-empty and unique")
        modes = tuple(mode for mode in AcceptanceMode if mode in supplied_modes)
        authorities = tuple(self.authorities)
        if any(not isinstance(item, ModelAuthority) for item in authorities):
            raise ValueError("capsule authorities must use ModelAuthority records")
        if len(authorities) != len(modes) or {item.mode for item in authorities} != set(modes):
            raise ValueError("capsule authorities must cover each acceptance mode exactly once")
        if len({item.role for item in authorities}) != len(authorities):
            raise ValueError("capsule authorities must be distinct")
        authority_by_mode = {item.mode: item.role for item in authorities}
        if any(authority_by_mode[mode] != _CANONICAL_ACCEPTANCE_ROLES[mode] for mode in modes):
            raise ValueError("capsule authorities must use canonical mode and role pairs")
        if isinstance(self.prompt_budget_bytes, bool) or not isinstance(self.prompt_budget_bytes, int):
            raise ValueError("capsule prompt budget must be an integer")
        if self.prompt_budget_bytes <= 0 or len(self.prompt.encode("utf-8")) > self.prompt_budget_bytes:
            raise ValueError("capsule prompt exceeds its measured byte budget")
        object.__setattr__(self, "decomposition", decomposition)
        object.__setattr__(self, "acceptance_modes", modes)
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "mutable_surfaces", mutable)
        object.__setattr__(self, "protected_surfaces", protected)
        object.__setattr__(self, "authorities", tuple(ModelAuthority(mode, authority_by_mode[mode]) for mode in modes))
        requirements = tuple(self.plugin_requirements)
        if any(not isinstance(item, PluginRequirement) for item in requirements):
            raise ValueError("capsule plugin requirements must use PluginRequirement records")
        if len(requirements) > _MAX_ITEMS:
            raise ValueError("capsule plugin requirements exceed the item limit")
        if len({item.canonical_id for item in requirements}) != len(requirements):
            raise ValueError("capsule plugin requirements must contain unique canonical ids")
        if self.schema_version == 1 and requirements:
            raise ValueError("schema-v1 capsules cannot imply plugin requirements")
        if self.schema_version == 2 and not requirements:
            raise ValueError("schema-v2 capsules require explicit plugin requirements")
        image_paths = tuple(self.local_image_paths)
        if self.schema_version in {1, 2} and image_paths:
            raise ValueError("schema-v1/v2 capsules cannot carry local image paths")
        if self.schema_version == 3 and not 1 <= len(image_paths) <= MAX_LOCAL_IMAGE_COUNT:
            raise ValueError("schema-v3 capsules require one to eight local image paths")
        if any(not isinstance(item, str) for item in image_paths):
            raise ValueError("capsule local image paths must be strings")
        try:
            tuple(LocalImageInput(item) for item in image_paths)
        except (TypeError, ValueError) as exc:
            raise ValueError("capsule local image paths are malformed") from exc
        object.__setattr__(self, "plugin_requirements", requirements)
        object.__setattr__(self, "local_image_paths", image_paths)

    def to_json(self) -> JsonObject:
        """Return the controller's detached JSON projection."""

        value: JsonObject = {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "decomposition": list(self.decomposition),
            "acceptance_criteria": list(self.acceptance_criteria),
            "surfaces": {
                **dict.fromkeys(self.mutable_surfaces, "mutable"),
                **dict.fromkeys(self.protected_surfaces, "protected"),
            },
            "acceptance": {item.mode.value: str(item.role) for item in self.authorities},
            "prompt": self.prompt,
            "recovery_policy": self.recovery_policy,
        }
        if self.schema_version == 2 or (self.schema_version == 3 and self.plugin_requirements):
            value["plugin_requirements"] = [item.to_json() for item in self.plugin_requirements]
        if self.schema_version == 3:
            value["local_image_paths"] = list(self.local_image_paths)
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ModelFacingCapsule:
        """Parse exactly one model-facing capsule shape; no defaults are guessed."""

        expected = {
            "schema_version",
            "objective",
            "decomposition",
            "acceptance_criteria",
            "surfaces",
            "acceptance",
            "prompt",
            "recovery_policy",
        }
        raw_version = value.get("schema_version")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version not in {1, 2, 3}:
            raise ValueError("model-facing capsule schema_version is unsupported")
        if raw_version == 1:
            accepted = expected
        elif raw_version == 2:
            accepted = expected | {"plugin_requirements"}
        else:
            accepted = expected | {"local_image_paths"}
            if "plugin_requirements" in value:
                accepted.add("plugin_requirements")
        if set(value) != accepted:
            raise ValueError("model-facing capsule keys do not match the selected schema version")

        def strings(key: str) -> tuple[str, ...]:
            raw = value[key]
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ValueError(f"capsule {key} must be an array of strings")
            return tuple(raw)

        raw_surfaces = value["surfaces"]
        if not isinstance(raw_surfaces, Mapping) or any(
            not isinstance(surface, str) or ownership not in {"mutable", "protected"}
            for surface, ownership in raw_surfaces.items()
        ):
            raise ValueError("capsule surfaces must map strings to mutable or protected")
        raw_acceptance = value["acceptance"]
        if not isinstance(raw_acceptance, Mapping) or any(
            not isinstance(mode, str) or not isinstance(role, str) for mode, role in raw_acceptance.items()
        ):
            raise ValueError("capsule acceptance must map supported modes to canonical roles")
        try:
            acceptance = {AcceptanceMode(mode): RoleId(role) for mode, role in raw_acceptance.items()}
        except ValueError as exc:
            raise ValueError("capsule acceptance contains an unsupported mode or role") from exc
        if not acceptance or any(acceptance[mode] != _CANONICAL_ACCEPTANCE_ROLES[mode] for mode in acceptance):
            raise ValueError("capsule acceptance must use canonical mode and role pairs")
        version, objective, prompt, recovery = (
            value["schema_version"],
            value["objective"],
            value["prompt"],
            value["recovery_policy"],
        )
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("capsule schema_version must be an integer")
        if not isinstance(objective, str) or not isinstance(prompt, str) or not isinstance(recovery, str):
            raise ValueError("capsule text fields must be strings")
        modes = tuple(mode for mode in AcceptanceMode if mode in acceptance)
        raw_requirements = value.get("plugin_requirements", [])
        if not isinstance(raw_requirements, list) or any(not isinstance(item, Mapping) for item in raw_requirements):
            raise ValueError("capsule plugin_requirements must be an array of objects")
        requirements = tuple(PluginRequirement.from_json(item) for item in raw_requirements)
        raw_images = value.get("local_image_paths", [])
        if not isinstance(raw_images, list) or any(not isinstance(item, str) for item in raw_images):
            raise ValueError("capsule local_image_paths must be an array of strings")
        return cls(
            version,
            objective,
            strings("decomposition"),
            modes,
            strings("acceptance_criteria"),
            tuple(surface for surface, ownership in raw_surfaces.items() if ownership == "mutable"),
            tuple(surface for surface, ownership in raw_surfaces.items() if ownership == "protected"),
            tuple(ModelAuthority(mode, acceptance[mode]) for mode in modes),
            prompt,
            recovery,
            _MAX_AGENT_MESSAGE_BYTES,
            requirements,
            tuple(raw_images),
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
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != 1
        ):
            raise ValueError("unsupported model-facing result schema version")
        if not isinstance(self.status, ModelResultStatus):
            object.__setattr__(self, "status", ModelResultStatus(self.status))
        _text(self.summary, label="result summary")
        _text(self.durable_status, label="result durable status", limit=128)
        if self.next_action is not None:
            _text(self.next_action, label="result next action")
        object.__setattr__(self, "changed_surfaces", _items(self.changed_surfaces, label="result changed surfaces"))
        validations = tuple(self.validations)
        if len(validations) > _MAX_ITEMS or any(not isinstance(item, ModelValidation) for item in validations):
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

    @classmethod
    def from_agent_message(cls, value: str | bytes | bytearray) -> ModelFacingResult:
        """Parse one complete raw terminal ``agentMessage`` payload.

        The terminal message is the sole model-facing authority for the
        App-native path.  Decoding is deliberately bounded and strict before
        the closed typed result parser is entered; summaries, Markdown fences,
        concatenated objects, and host-authored projections therefore cannot be
        accepted as a result.
        """

        decoded = strict_json_loads(value, max_bytes=_MAX_AGENT_MESSAGE_BYTES)
        if not isinstance(decoded, Mapping):
            raise ValueError("raw agentMessage result root must be an object")
        return cls.from_json(decoded)


# ---------------------------------------------------------------------------
# Closed controller-decision action contract (schema version 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelFacingControllerAction:
    """One closed action selected by a controller model."""

    kind: ControllerActionKind
    dispatch_id: DispatchId | None = None
    action_id: str | None = None
    expected_retry_revision: int | None = None
    requested_retry_budget: RetryBudgetChange | None = None
    checkpoint_seconds: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ControllerActionKind):
            object.__setattr__(self, "kind", ControllerActionKind(self.kind))
        if self.dispatch_id is not None and not isinstance(self.dispatch_id, DispatchId):
            object.__setattr__(self, "dispatch_id", DispatchId(self.dispatch_id))
        if self.action_id is not None:
            _text(self.action_id, label="controller action id", limit=256)
        if self.expected_retry_revision is not None and (
            isinstance(self.expected_retry_revision, bool)
            or not isinstance(self.expected_retry_revision, int)
            or self.expected_retry_revision < 0
        ):
            raise ValueError("controller retry revision is invalid")
        if self.checkpoint_seconds is not None and (
            isinstance(self.checkpoint_seconds, bool)
            or not isinstance(self.checkpoint_seconds, int)
            or not 1 <= self.checkpoint_seconds <= 86_400
        ):
            raise ValueError("controller checkpoint duration is invalid")
        if self.reason is not None:
            _text(self.reason, label="controller action reason", limit=512)
        if self.kind is ControllerActionKind.CHANGE_RETRY_BUDGET and self.requested_retry_budget is None:
            raise ValueError("change_retry_budget requires requested_retry_budget")
        if self.kind is not ControllerActionKind.CHANGE_RETRY_BUDGET and self.requested_retry_budget is not None:
            raise ValueError("requested_retry_budget is only valid for change_retry_budget")
        if self.kind in {
            ControllerActionKind.RETRY_DISPATCH,
            ControllerActionKind.CANCEL_DISPATCH,
            ControllerActionKind.CHANGE_RETRY_BUDGET,
        }:
            if self.dispatch_id is None or self.action_id is None or self.expected_retry_revision is None:
                raise ValueError("dispatch recovery actions require action id, dispatch and retry revision")
        if self.kind is ControllerActionKind.REARM_CHECKPOINT and self.checkpoint_seconds is None:
            raise ValueError("rearm_checkpoint requires checkpoint_seconds")
        if self.kind is ControllerActionKind.REQUIRE_HUMAN_ATTENTION and self.reason is None:
            raise ValueError("require_human_attention requires a reason")
        if self.kind is ControllerActionKind.ACKNOWLEDGE_ONLY and any(
            value is not None
            for value in (
                self.dispatch_id,
                self.action_id,
                self.expected_retry_revision,
                self.requested_retry_budget,
                self.checkpoint_seconds,
            )
        ):
            raise ValueError("acknowledge_only cannot carry action parameters")

    def to_json(self) -> JsonObject:
        value: JsonObject = {"kind": self.kind.value}
        if self.dispatch_id is not None:
            value["dispatch_id"] = str(self.dispatch_id)
        if self.action_id is not None:
            value["action_id"] = self.action_id
        if self.expected_retry_revision is not None:
            value["expected_retry_revision"] = self.expected_retry_revision
        if self.requested_retry_budget is not None:
            value["requested_retry_budget"] = self.requested_retry_budget.to_json()
        if self.checkpoint_seconds is not None:
            value["checkpoint_seconds"] = self.checkpoint_seconds
        if self.reason is not None:
            value["reason"] = self.reason
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ModelFacingControllerAction:
        if not isinstance(value, Mapping) or "kind" not in value:
            raise ValueError("controller action must be an object with kind")
        kind_value = value["kind"]
        if not isinstance(kind_value, str):
            raise ValueError("controller action kind must be a string")
        try:
            kind = ControllerActionKind(kind_value)
        except ValueError as exc:
            raise ValueError("controller action kind is unsupported") from exc
        allowed = {
            ControllerActionKind.ACKNOWLEDGE_ONLY: {"kind"},
            ControllerActionKind.REARM_CHECKPOINT: {"kind", "checkpoint_seconds"},
            ControllerActionKind.RETRY_DISPATCH: {"kind", "dispatch_id", "action_id", "expected_retry_revision"},
            ControllerActionKind.CANCEL_DISPATCH: {"kind", "dispatch_id", "action_id", "expected_retry_revision"},
            ControllerActionKind.CHANGE_RETRY_BUDGET: {
                "kind",
                "dispatch_id",
                "action_id",
                "expected_retry_revision",
                "requested_retry_budget",
            },
            ControllerActionKind.REQUIRE_HUMAN_ATTENTION: {"kind", "reason"},
        }[kind]
        if set(value) != allowed:
            raise ValueError("controller action has an unsupported shape")
        dispatch = value.get("dispatch_id")
        if dispatch is not None and not isinstance(dispatch, str):
            raise ValueError("controller action dispatch id is invalid")
        budget = value.get("requested_retry_budget")
        parsed_budget = None
        if budget is not None:
            if not isinstance(budget, Mapping) or set(budget) != {
                "pre_identity_budget",
                "invalid_chain_budget",
                "schema_envelope_budget",
                "post_identity_loss_budget",
            }:
                raise ValueError("controller retry budget has an unsupported shape")
            parsed_budget = RetryBudgetChange(**budget)  # type: ignore[arg-type]
        return cls(
            kind,
            DispatchId(dispatch) if dispatch is not None else None,
            value.get("action_id"),  # type: ignore[arg-type]
            value.get("expected_retry_revision"),  # type: ignore[arg-type]
            parsed_budget,
            value.get("checkpoint_seconds"),  # type: ignore[arg-type]
            value.get("reason"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ModelFacingControllerActionBundle:
    """The only model-authored mutation envelope for a controller decision."""

    schema_version: int
    decision_id: ControllerDecisionId
    generation: Generation
    action_id: str
    expected_revision: int
    expected_successor_dispatch_ids: tuple[str, ...]
    actions: tuple[ModelFacingControllerAction, ...]
    rationale: str

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("unsupported controller action schema version")
        if not isinstance(self.decision_id, ControllerDecisionId):
            object.__setattr__(self, "decision_id", ControllerDecisionId(self.decision_id))
        if not isinstance(self.generation, Generation):
            object.__setattr__(self, "generation", Generation(self.generation))
        _text(self.action_id, label="controller bundle action id", limit=256)
        if (
            isinstance(self.expected_revision, bool)
            or not isinstance(self.expected_revision, int)
            or self.expected_revision < 0
        ):
            raise ValueError("controller bundle revision is invalid")
        successors = tuple(self.expected_successor_dispatch_ids)
        if len(successors) > 128 or len(set(successors)) != len(successors):
            raise ValueError("controller successor snapshot is invalid")
        for dispatch in successors:
            DispatchId(dispatch)
        object.__setattr__(self, "expected_successor_dispatch_ids", tuple(sorted(successors)))
        actions = tuple(self.actions)
        if not 1 <= len(actions) <= 8 or any(not isinstance(item, ModelFacingControllerAction) for item in actions):
            raise ValueError("controller bundle must contain one to eight typed actions")
        kinds = {item.kind for item in actions}
        if len(kinds) != len(actions):
            raise ValueError("controller action kinds must be unique")
        if ControllerActionKind.ACKNOWLEDGE_ONLY in kinds and len(actions) != 1:
            raise ValueError("acknowledge_only is exclusive")
        if ControllerActionKind.REARM_CHECKPOINT in kinds and kinds & {
            ControllerActionKind.CANCEL_DISPATCH,
            ControllerActionKind.REQUIRE_HUMAN_ATTENTION,
        }:
            raise ValueError("checkpoint re-arm conflicts with cancellation or attention")
        if ControllerActionKind.REQUIRE_HUMAN_ATTENTION in kinds and len(actions) != 1:
            raise ValueError("human-attention action is exclusive")
        recovery_kinds = kinds & {
            ControllerActionKind.RETRY_DISPATCH,
            ControllerActionKind.CANCEL_DISPATCH,
            ControllerActionKind.CHANGE_RETRY_BUDGET,
        }
        if len(recovery_kinds) > 1:
            raise ValueError("controller recovery actions are mutually exclusive")
        if len({item.action_id for item in actions if item.action_id is not None}) != len(
            [item for item in actions if item.action_id is not None]
        ):
            raise ValueError("controller action ids must be unique")
        object.__setattr__(self, "actions", actions)
        _text(self.rationale, label="controller bundle rationale", limit=4_096)
        if len(self.to_json_bytes()) > 32 * 1024:
            raise ValueError("controller action bundle exceeds its byte limit")

    def to_json(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "decision_id": str(self.decision_id),
            "generation": int(self.generation),
            "action_id": self.action_id,
            "expected_revision": self.expected_revision,
            "expected_successor_dispatch_ids": list(self.expected_successor_dispatch_ids),
            "actions": [item.to_json() for item in self.actions],
            "rationale": self.rationale,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> ModelFacingControllerActionBundle:
        expected = {
            "schema_version",
            "decision_id",
            "generation",
            "action_id",
            "expected_revision",
            "expected_successor_dispatch_ids",
            "actions",
            "rationale",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("controller action bundle keys are unsupported")
        actions = value["actions"]
        successors = value["expected_successor_dispatch_ids"]
        if (
            not isinstance(actions, list)
            or not isinstance(successors, list)
            or any(not isinstance(item, Mapping) for item in actions)
            or any(not isinstance(item, str) for item in successors)
        ):
            raise ValueError("controller bundle arrays are malformed")
        return cls(
            value["schema_version"],  # type: ignore[arg-type]
            value["decision_id"],  # type: ignore[arg-type]
            value["generation"],  # type: ignore[arg-type]
            value["action_id"],  # type: ignore[arg-type]
            value["expected_revision"],  # type: ignore[arg-type]
            tuple(successors),
            tuple(ModelFacingControllerAction.from_json(item) for item in actions),
            value["rationale"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_json_bytes(cls, value: str | bytes | bytearray) -> ModelFacingControllerActionBundle:
        decoded = strict_json_loads(value, max_bytes=32 * 1024)
        if not isinstance(decoded, Mapping):
            raise ValueError("controller action bundle root must be an object")
        return cls.from_json(decoded)


def model_facing_capsule_schema() -> JsonObject:
    """Return the closed JSON schema used when serializing a capsule."""

    text = {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT, "pattern": _TEXT_PATTERN}
    text_array = {"type": "array", "maxItems": _MAX_ITEMS, "uniqueItems": True, "items": text}
    plugin_requirement = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "canonical_id",
            "version",
            "source",
            "bundle_digest",
            "bundled_skill_ids",
            "mcp_connectors",
        ],
        "properties": {
            "canonical_id": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _TEXT_PATTERN},
            "version": {"type": "string", "minLength": 1, "maxLength": 128, "pattern": _TEXT_PATTERN},
            "source": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": _TEXT_PATTERN},
            "bundle_digest": {"type": "string", "pattern": _SHA256_PATTERN.pattern},
            "bundled_skill_ids": text_array,
            "mcp_connectors": text_array,
        },
    }
    common_properties = {
        "objective": text,
        "decomposition": text_array,
        "acceptance_criteria": text_array,
        "surfaces": {
            "type": "object",
            "minProperties": 0,
            "maxProperties": _MAX_ITEMS,
            "propertyNames": text,
            "additionalProperties": {"type": "string", "enum": ["mutable", "protected"]},
        },
        "acceptance": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": len(AcceptanceMode),
            "additionalProperties": False,
            "properties": {
                "objective": {"type": "string", "const": "code-reviewer"},
                "visual": {"type": "string", "const": "visual-reviewer"},
                "architecture": {"type": "string", "const": "architecture-reviewer"},
            },
        },
        "prompt": text,
        "recovery_policy": {"type": "string", "const": "completion_biased"},
    }
    common_required = [
        "schema_version",
        "objective",
        "decomposition",
        "acceptance_criteria",
        "surfaces",
        "acceptance",
        "prompt",
        "recovery_policy",
    ]

    def version_schema(
        version: int, *, include_plugins: bool, image_paths: bool = False, optional_plugins: bool = False
    ) -> JsonObject:
        properties: JsonObject = {"schema_version": {"type": "integer", "const": version}, **common_properties}
        required = list(common_required)
        if image_paths:
            properties["local_image_paths"] = {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_LOCAL_IMAGE_COUNT,
                "uniqueItems": True,
                "items": {
                    **text,
                    "maxLength": 4_096,
                    "pattern": r"^(?!/)(?![\s\S]*\u0000)(?=[\s\S]*\S)",
                },
            }
            required.append("local_image_paths")
        if include_plugins:
            properties["plugin_requirements"] = {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_ITEMS,
                "uniqueItems": True,
                "items": plugin_requirement,
            }
            if not optional_plugins:
                required.append("plugin_requirements")
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://personal-workflow-skills.invalid/schemas/capsule.schema.json",
        "title": "Controller-facing model capsule",
        "oneOf": [
            version_schema(1, include_plugins=False),
            version_schema(2, include_plugins=True),
            version_schema(3, include_plugins=True, image_paths=True, optional_plugins=True),
        ],
    }


def model_facing_controller_action_schema() -> JsonObject:
    """Return the closed schema for controller action bundles."""

    action_base = {
        "type": "object",
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://personal-workflow-skills.invalid/schemas/controller-action.schema.json",
        "title": "Controller decision action bundle",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "decision_id",
            "generation",
            "action_id",
            "expected_revision",
            "expected_successor_dispatch_ids",
            "actions",
            "rationale",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "decision_id": {"type": "string", "pattern": r"^decision/[A-Za-z0-9][A-Za-z0-9_./-]*$"},
            "generation": {"type": "integer", "minimum": 1, "maximum": 2},
            "action_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "expected_revision": {"type": "integer", "minimum": 0},
            "expected_successor_dispatch_ids": {
                "type": "array",
                "maxItems": 128,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
            },
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "oneOf": [
                        {**action_base, "required": ["kind"], "properties": {"kind": {"const": "acknowledge_only"}}},
                        {
                            **action_base,
                            "required": ["kind", "checkpoint_seconds"],
                            "properties": {
                                "kind": {"const": "rearm_checkpoint"},
                                "checkpoint_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                            },
                        },
                        *[
                            {
                                **action_base,
                                "required": ["kind", "dispatch_id", "action_id", "expected_retry_revision"],
                                "properties": {
                                    "kind": {"const": kind},
                                    "dispatch_id": {"type": "string", "minLength": 1, "maxLength": 512},
                                    "action_id": {"type": "string", "minLength": 1, "maxLength": 256},
                                    "expected_retry_revision": {"type": "integer", "minimum": 0},
                                },
                            }
                            for kind in ("retry_dispatch", "cancel_dispatch")
                        ],
                        {
                            **action_base,
                            "required": [
                                "kind",
                                "dispatch_id",
                                "action_id",
                                "expected_retry_revision",
                                "requested_retry_budget",
                            ],
                            "properties": {
                                "kind": {"const": "change_retry_budget"},
                                "dispatch_id": {"type": "string", "minLength": 1, "maxLength": 512},
                                "action_id": {"type": "string", "minLength": 1, "maxLength": 256},
                                "expected_retry_revision": {"type": "integer", "minimum": 0},
                                "requested_retry_budget": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "pre_identity_budget",
                                        "invalid_chain_budget",
                                        "schema_envelope_budget",
                                        "post_identity_loss_budget",
                                    ],
                                    "properties": {
                                        name: {"type": "integer", "minimum": 0}
                                        for name in (
                                            "pre_identity_budget",
                                            "invalid_chain_budget",
                                            "schema_envelope_budget",
                                            "post_identity_loss_budget",
                                        )
                                    },
                                },
                            },
                        },
                        {
                            **action_base,
                            "required": ["kind", "reason"],
                            "properties": {"kind": {"const": "require_human_attention"}, "reason": {"type": "string"}},
                        },
                    ]
                },
            },
            "rationale": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
    }


def model_facing_result_schema() -> JsonObject:
    """Return the closed JSON schema used when serializing a result."""

    text = {"type": "string", "minLength": 1, "maxLength": _MAX_TEXT, "pattern": _TEXT_PATTERN}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://personal-workflow-skills.invalid/schemas/result.schema.json",
        "title": "Controller-facing model result",
        "description": "The closed durable result projection emitted at the model boundary.",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "status",
            "summary",
            "changed_surfaces",
            "validations",
            "durable_status",
            "next_action",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "status": {"type": "string", "enum": [status.value for status in ModelResultStatus]},
            "summary": text,
            "changed_surfaces": {"type": "array", "maxItems": _MAX_ITEMS, "uniqueItems": True, "items": text},
            "validations": {
                "type": "array",
                "maxItems": _MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "passed", "evidence"],
                    "properties": {
                        "name": {**text, "maxLength": 256},
                        "passed": {"type": "boolean"},
                        "evidence": {**text, "maxLength": 512},
                    },
                },
            },
            "durable_status": {**text, "maxLength": 128},
            "next_action": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": _MAX_TEXT,
                "pattern": _TEXT_PATTERN,
            },
        },
    }


def model_facing_result_schema_sha256() -> str:
    """Return the digest bound into new App-native host actions."""

    encoded = json.dumps(
        model_facing_result_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def format_model_facing_result_prompt(prompt: str) -> str:
    """Append the one canonical schema-version-1 result envelope to a prompt.

    Native task creation has no structured-output argument, so the formatter
    carries the closed contract in-band.  The placeholder object is stable and
    intentionally compact; workers must emit one JSON object alone, without
    prose or a Markdown fence.
    """

    _text(prompt, label="native task prompt")
    envelope = {
        "schema_version": 1,
        "status": "completed",
        "summary": "<bounded summary>",
        "changed_surfaces": [],
        "validations": [],
        "durable_status": "<durable status>",
        "next_action": None,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (
        prompt.rstrip()
        + "\n\nCodex Flow terminal result envelope (ModelFacingResult schema_version=1):\n"
        + encoded
        + "\nEmit exactly this JSON object alone; do not add prose or a Markdown fence."
    )


__all__ = [
    "ModelAuthority",
    "ModelFacingCapsule",
    "ModelFacingControllerAction",
    "ModelFacingControllerActionBundle",
    "ModelFacingResult",
    "ModelResultStatus",
    "ModelValidation",
    "PluginCapabilitySnapshot",
    "PluginReadiness",
    "PluginRequirement",
    "format_model_facing_result_prompt",
    "model_facing_capsule_schema",
    "model_facing_controller_action_schema",
    "model_facing_result_schema",
    "model_facing_result_schema_sha256",
]
