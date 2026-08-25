"""Typed routing and limit configuration for the workflow controller.

``workflow.toml`` is the only model/effort authority.  Configuration is loaded
at the boundary and never silently substitutes a missing or invalid route.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .domain import AcceptanceMode, AuthorityAssignment, AuthorityPlan, Budget, ReasoningEffort, RoleId


class WorkflowConfigError(ValueError):
    """The versioned workflow configuration is missing or malformed."""


class AuthorityUnavailable(WorkflowConfigError):
    """A required authority cannot be dispatched under the frozen routes."""


@dataclass(frozen=True, slots=True)
class RoleRoute:
    role: RoleId
    model: str
    reasoning_effort: ReasoningEffort

    def __post_init__(self) -> None:
        if not isinstance(self.role, RoleId):
            object.__setattr__(self, "role", RoleId(self.role))
        if not isinstance(self.model, str) or not self.model.strip() or "\x00" in self.model:
            raise WorkflowConfigError("role model must be a bounded non-empty string")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            object.__setattr__(self, "reasoning_effort", ReasoningEffort(self.reasoning_effort))


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    max_turns: int
    max_repairs: int
    max_compactions: int
    max_validations: int
    max_reviews: int
    wall_clock_seconds: float

    def __post_init__(self) -> None:
        for name in ("max_turns", "max_repairs", "max_compactions", "max_validations", "max_reviews"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WorkflowConfigError(f"{name} must be a non-negative integer")
        if not math.isfinite(self.wall_clock_seconds) or self.wall_clock_seconds <= 0:
            raise WorkflowConfigError("wall_clock_seconds must be positive")

    def budget(self) -> Budget:
        return Budget(
            max_turns=self.max_turns,
            max_repairs=self.max_repairs,
            max_reviews=self.max_reviews,
            max_compactions=self.max_compactions,
            max_validations=self.max_validations,
            wall_clock_seconds=self.wall_clock_seconds,
        )


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    version: int
    roles: Mapping[RoleId, RoleRoute]
    limits: WorkflowLimits

    def __post_init__(self) -> None:
        if self.version != 1:
            raise WorkflowConfigError("unsupported workflow configuration version")
        expected = {
            "planner",
            "executor",
            "code-reviewer",
            "visual-reviewer",
            "architecture-reviewer",
            "recovery",
            "decision",
        }
        actual = {str(role) for role in self.roles}
        if actual != expected:
            raise WorkflowConfigError(f"workflow roles must be exactly {sorted(expected)!r}")
        normalized = {RoleId(role): route for role, route in self.roles.items()}
        object.__setattr__(self, "roles", MappingProxyType(normalized))

    def route(self, role: RoleId | str) -> RoleRoute:
        try:
            return self.roles[RoleId(role)]
        except KeyError as exc:
            raise WorkflowConfigError(f"workflow role is not configured: {role!r}") from exc

    def validate_runtime(self, *, available_models: set[str] | None = None) -> None:
        """Fail closed when a configured route is not available; never substitute."""

        aliases = sorted(str(role) for role, route in self.roles.items() if route.role != role)
        if aliases:
            raise AuthorityUnavailable(f"configured role routes are aliased: {aliases!r}")
        if available_models is None:
            return
        missing = sorted({route.model for route in self.roles.values()} - available_models)
        if missing:
            raise WorkflowConfigError(f"configured model route is unavailable: {missing!r}")

    def derive_authorities(
        self,
        acceptance_modes: Sequence[AcceptanceMode | str],
        *,
        available_models: set[str] | None = None,
    ) -> AuthorityPlan:
        """Derive all authorities for a capsule and fail closed on drift.

        The mapping is deliberately explicit.  A reviewer role may not be
        reused for another required acceptance mode, and a route's declared
        role must agree with its table key.  No model, effort, provider, or
        transport fallback is attempted here.
        """

        modes = tuple(mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in acceptance_modes)
        if not modes or len(modes) != len(set(modes)):
            raise AuthorityUnavailable("acceptance modes must be non-empty and unique")
        required: list[tuple[AcceptanceMode | None, RoleId]] = [(None, RoleId("executor"))]
        if AcceptanceMode.OBJECTIVE in modes:
            required.append((AcceptanceMode.OBJECTIVE, RoleId("code-reviewer")))
        if AcceptanceMode.VISUAL in modes:
            required.append((AcceptanceMode.VISUAL, RoleId("visual-reviewer")))
        if AcceptanceMode.ARCHITECTURE in modes:
            required.append((AcceptanceMode.ARCHITECTURE, RoleId("architecture-reviewer")))
        # Recovery and decision are always part of the H4 authority graph:
        # they are needed to classify a rejection even when the happy path
        # never invokes them.
        required.extend(((None, RoleId("recovery")), (None, RoleId("decision"))))
        assignments: list[AuthorityAssignment] = []
        for mode, role in required:
            route = self.route(role)
            if route.role != role:
                raise AuthorityUnavailable(f"configured route for {role} is aliased to {route.role}")
            if available_models is not None and route.model not in available_models:
                raise AuthorityUnavailable(f"configured model route is unavailable: {route.model!r}")
            assignments.append(AuthorityAssignment(mode, role, route.model, route.reasoning_effort))
        try:
            return AuthorityPlan(tuple(assignments))
        except ValueError as exc:
            raise AuthorityUnavailable(str(exc)) from exc


def _table(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise WorkflowConfigError(f"{label} must be a table")
    return value


def _integer(table: Mapping[str, object], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowConfigError(f"{key} must be an integer")
    return value


def load_workflow_config(path: str | Path = "workflow.toml") -> WorkflowConfig:
    """Load and validate the repository-owned routing/limit table."""

    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkflowConfigError(f"unable to read workflow configuration: {source}") from exc
    if set(raw) != {"version", "roles", "limits"}:
        raise WorkflowConfigError("workflow configuration has unsupported top-level keys")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise WorkflowConfigError("workflow configuration version must be an integer")
    role_table = _table(raw.get("roles"), label="roles")
    routes: dict[RoleId, RoleRoute] = {}
    for role_name, value in role_table.items():
        role = RoleId(str(role_name))
        fields = _table(value, label=f"roles.{role}")
        if set(fields) != {"model", "reasoning_effort"}:
            raise WorkflowConfigError(f"roles.{role} has unsupported keys")
        model = fields.get("model")
        effort = fields.get("reasoning_effort")
        if not isinstance(model, str) or not isinstance(effort, str):
            raise WorkflowConfigError(f"roles.{role} requires model and reasoning_effort")
        try:
            routes[role] = RoleRoute(role, model, ReasoningEffort(effort))
        except ValueError as exc:
            raise WorkflowConfigError(f"roles.{role} has an invalid typed route") from exc
    limits = _table(raw.get("limits"), label="limits")
    if set(limits) != {
        "max_turns",
        "max_repairs",
        "max_compactions",
        "max_validations",
        "max_reviews",
        "wall_clock_seconds",
    }:
        raise WorkflowConfigError("limits has unsupported keys")
    wall_clock = limits.get("wall_clock_seconds")
    if isinstance(wall_clock, bool) or not isinstance(wall_clock, int | float):
        raise WorkflowConfigError("wall_clock_seconds must be numeric")
    try:
        typed_limits = WorkflowLimits(
            max_turns=_integer(limits, "max_turns"),
            max_repairs=_integer(limits, "max_repairs"),
            max_compactions=_integer(limits, "max_compactions"),
            max_validations=_integer(limits, "max_validations"),
            max_reviews=_integer(limits, "max_reviews"),
            wall_clock_seconds=float(wall_clock),
        )
        return WorkflowConfig(version, routes, typed_limits)
    except (TypeError, ValueError) as exc:
        raise WorkflowConfigError("workflow limits are invalid") from exc
