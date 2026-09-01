"""Closed model-facing capsule projection into the H3 execution boundary.

The model-facing contract deliberately does not let a model choose a checkout,
route, permission, or validation command.  This module is the one boundary that
binds those facts from the selected checkout and the repository-owned
``workflow.toml`` before the existing :class:`ExecutionCapsule` controller is
constructed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Final

from .config import AuthorityUnavailable, WorkflowConfig, WorkflowConfigError, load_workflow_config
from .contracts import ModelFacingCapsule, model_facing_result_schema
from .controller import capsule_from_json
from .domain import (
    ExecutionCapsule,
    MilestoneId,
    NativePermissionMode,
    RunId,
    ValidationSpec,
    WorkspaceMode,
    strict_json_loads,
)


class ProjectionError(ValueError):
    """A model-facing capsule cannot be projected safely."""


_MODEL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "objective",
        "decomposition",
        "acceptance_criteria",
        "surfaces",
        "acceptance",
        "prompt",
        "recovery_policy",
    }
)
_MODEL_KEYS_WITH_PLUGINS: Final[frozenset[str]] = _MODEL_KEYS | {"plugin_requirements"}
_MODEL_KEYS_WITH_IMAGES: Final[frozenset[str]] = _MODEL_KEYS | {"local_image_paths"}
_MODEL_KEYS_WITH_IMAGES_AND_PLUGINS: Final[frozenset[str]] = _MODEL_KEYS_WITH_IMAGES | {"plugin_requirements"}
_EXECUTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "capsule_version",
        "run_id",
        "milestone_id",
        "repository_root",
        "workspace_mode",
        "workspace_path",
        "branch",
        "base_sha",
        "lane",
        "mutable_paths",
        "protected_paths",
        "validation",
        "model",
        "reasoning_effort",
        "prompt",
        "output_schema",
        "permission_mode",
    }
)
_EXECUTION_KEYS_WITH_MODES: Final[frozenset[str]] = _EXECUTION_KEYS | {"acceptance_modes"}
_EXECUTION_KEYS_WITH_PLUGINS: Final[frozenset[str]] = _EXECUTION_KEYS | {"plugin_requirements"}
_EXECUTION_KEYS_WITH_MODES_AND_PLUGINS: Final[frozenset[str]] = _EXECUTION_KEYS | {
    "acceptance_modes",
    "plugin_requirements",
}
_EXECUTION_KEYS_WITH_IMAGES: Final[frozenset[str]] = _EXECUTION_KEYS | {"local_image_paths"}
_EXECUTION_KEYS_WITH_IMAGES_AND_MODES: Final[frozenset[str]] = _EXECUTION_KEYS_WITH_IMAGES | {"acceptance_modes"}
_EXECUTION_KEYS_WITH_IMAGES_AND_PLUGINS: Final[frozenset[str]] = _EXECUTION_KEYS_WITH_IMAGES | {"plugin_requirements"}
_EXECUTION_KEYS_WITH_IMAGES_MODES_AND_PLUGINS: Final[frozenset[str]] = _EXECUTION_KEYS_WITH_IMAGES | {
    "acceptance_modes",
    "plugin_requirements",
}
_DEFAULT_VALIDATION: Final[ValidationSpec] = ValidationSpec(("git", "diff", "--check"), 60.0)
_LANE: Final[str] = "model-facing"


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(("git", *arguments), cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise ProjectionError("unable to inspect the selected Git checkout")
    return result.stdout.strip()


def selected_checkout_facts(state_root: Path) -> tuple[Path, Path, WorkspaceMode, str, str, str]:
    """Resolve repository and selected-checkout topology without creating Git state."""
    try:
        checkout = state_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectionError("--state-root must identify an existing checkout") from exc
    if not checkout.is_dir():
        raise ProjectionError("--state-root must identify a directory")
    try:
        toplevel = Path(_git_output(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
        branch = _git_output(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        base_sha = _git_output(checkout, "rev-parse", "--verify", "HEAD^{commit}")
        common_directory = Path(
            _git_output(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectionError("--state-root must identify a Git checkout with a symbolic branch") from exc
    if toplevel != checkout:
        raise ProjectionError("--state-root must be the physical Git toplevel")
    if len(base_sha) != 40 or any(character not in "0123456789abcdef" for character in base_sha):
        raise ProjectionError("selected checkout HEAD is not a resolved Git SHA")
    repository = common_directory.parent.resolve(strict=True)
    if Path(_git_output(repository, "rev-parse", "--show-toplevel")).resolve(strict=True) != repository:
        raise ProjectionError("unable to resolve the primary repository checkout")
    mode = WorkspaceMode.CURRENT_CHECKOUT if repository == checkout else WorkspaceMode.EXISTING_WORKTREE
    lane = branch.removeprefix("agent/") if branch.startswith("agent/") else _LANE
    return repository, checkout, mode, branch, base_sha, lane


def _load_packaged_workflow_config() -> WorkflowConfig:
    """Load the wheel-bundled canonical config, or the source config in-tree."""

    try:
        resource = resources.files("codex_flow").joinpath("workflow.toml")
        if resource.is_file():
            with resources.as_file(resource) as path:
                return load_workflow_config(path)
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        pass
    source_path = Path(__file__).resolve().parents[2] / "workflow.toml"
    if source_path.is_file():
        return load_workflow_config(source_path)
    raise ProjectionError("canonical workflow.toml is unavailable")


def _workflow_config(checkout: Path) -> WorkflowConfig:
    checkout_config = checkout / "workflow.toml"
    if checkout_config.exists():
        try:
            return load_workflow_config(checkout_config)
        except WorkflowConfigError as exc:
            raise ProjectionError("checkout workflow.toml is invalid") from exc
    try:
        return _load_packaged_workflow_config()
    except WorkflowConfigError as exc:
        raise ProjectionError("canonical workflow.toml is invalid") from exc


def _config_identity(config: WorkflowConfig) -> dict[str, object]:
    return {
        "version": config.version,
        "roles": {
            str(role): {
                "model": route.model,
                "reasoning_effort": route.reasoning_effort.value,
            }
            for role, route in sorted(config.roles.items(), key=lambda item: str(item[0]))
        },
        "limits": {
            "max_turns": config.limits.max_turns,
            "max_repairs": config.limits.max_repairs,
            "max_compactions": config.limits.max_compactions,
            "max_validations": config.limits.max_validations,
            "max_reviews": config.limits.max_reviews,
            "wall_clock_seconds": config.limits.wall_clock_seconds,
        },
    }


def _validate_authorities(capsule: ModelFacingCapsule, config: WorkflowConfig) -> None:
    try:
        config.validate_runtime()
        config.derive_authorities(capsule.acceptance_modes)
        for authority in capsule.authorities:
            route = config.route(authority.role)
            if route.role != authority.role:
                raise AuthorityUnavailable(f"workflow route for {authority.role} is aliased")
    except (KeyError, ValueError, WorkflowConfigError) as exc:
        if isinstance(exc, AuthorityUnavailable):
            raise ProjectionError(str(exc)) from exc
        raise ProjectionError("model-facing authorities do not match workflow.toml") from exc


def _projection_prompt(capsule: ModelFacingCapsule) -> str:
    """Carry the model-facing intent into the only prompt field of H3."""

    context = {
        "objective": capsule.objective,
        "decomposition": list(capsule.decomposition),
        "acceptance_criteria": list(capsule.acceptance_criteria),
        "acceptance_modes": [mode.value for mode in capsule.acceptance_modes],
        "authorities": [{"mode": item.mode.value, "role": str(item.role)} for item in capsule.authorities],
        "mutable_surfaces": list(capsule.mutable_surfaces),
        "protected_surfaces": list(capsule.protected_surfaces),
        "recovery_policy": capsule.recovery_policy,
        "output_contract": "ModelFacingResult schema_version=1",
    }
    return (
        capsule.prompt
        + "\n\nCodex Flow model-facing intent (controller-owned JSON):\n"
        + _canonical_json(context).decode("utf-8")
        + "Git-ignored files, directories, symlinks, and build output are excluded from ordinary workspace "
        + "mutation and topology checks; tracked files and non-ignored untracked changes remain scope evidence. "
        + "Never remove pre-existing user artifacts.\n"
        + "Return exactly one ModelFacingResult matching the output schema."
    )


def project_model_facing_capsule(capsule: ModelFacingCapsule, *, state_root: Path) -> ExecutionCapsule:
    """Project one closed model-facing capsule into an H3 ``ExecutionCapsule``."""

    if not isinstance(capsule, ModelFacingCapsule):
        raise ProjectionError("projection requires an exact ModelFacingCapsule")
    repository, checkout, workspace_mode, branch, base_sha, lane = selected_checkout_facts(state_root)
    config = _workflow_config(checkout)
    _validate_authorities(capsule, config)
    executor = config.route("executor")
    if executor.role.value != "executor":
        raise ProjectionError("workflow executor route is aliased")
    identity = {
        "schema": "codex-flow/model-facing-projection/v1",
        "capsule": capsule.to_json(),
        "repository": str(repository),
        "checkout": str(checkout),
        "workspace_mode": workspace_mode.value,
        "lane": lane,
        "branch": branch,
        "base_sha": base_sha,
        "workflow": _config_identity(config),
    }
    digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    try:
        run_id = RunId(f"model-{digest[:32]}")
        milestone_id = MilestoneId(f"milestone-{digest[32:]}")
        return ExecutionCapsule(
            3 if capsule.schema_version == 3 else 2,
            run_id,
            milestone_id,
            repository,
            workspace_mode,
            checkout,
            branch,
            base_sha,
            lane,
            capsule.mutable_surfaces,
            capsule.protected_surfaces,
            _DEFAULT_VALIDATION,
            executor.model,
            executor.reasoning_effort,
            _projection_prompt(capsule),
            model_facing_result_schema(),
            NativePermissionMode.INHERIT_NATIVE,
            capsule.acceptance_modes,
            tuple(item.to_json() for item in capsule.plugin_requirements),
            capsule.local_image_paths,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectionError("model-facing capsule cannot be projected into an execution capsule") from exc


def load_control_capsule(path: Path, *, state_root: Path) -> tuple[ExecutionCapsule, str]:
    """Load either the exact model-facing shape or the explicit internal shape.

    Shape classification is exact and performed before any controller object is
    constructed.  A mixed or unknown object is never guessed to be one of the
    two accepted contracts.
    """

    try:
        raw = path.read_bytes()
        decoded = strict_json_loads(raw)
    except (OSError, ValueError) as exc:
        raise ProjectionError(f"capsule cannot be decoded: {path}") from exc
    if not isinstance(decoded, Mapping):
        raise ProjectionError("capsule root must be an object")
    keys = frozenset(decoded)
    if keys in {
        _MODEL_KEYS,
        _MODEL_KEYS_WITH_PLUGINS,
        _MODEL_KEYS_WITH_IMAGES,
        _MODEL_KEYS_WITH_IMAGES_AND_PLUGINS,
    }:
        try:
            return project_model_facing_capsule(
                ModelFacingCapsule.from_json(decoded), state_root=state_root
            ), hashlib.sha256(raw).hexdigest()
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ProjectionError):
                raise
            raise ProjectionError("model-facing capsule is malformed") from exc
    if keys in {
        _EXECUTION_KEYS,
        _EXECUTION_KEYS_WITH_MODES,
        _EXECUTION_KEYS_WITH_PLUGINS,
        _EXECUTION_KEYS_WITH_MODES_AND_PLUGINS,
        _EXECUTION_KEYS_WITH_IMAGES,
        _EXECUTION_KEYS_WITH_IMAGES_AND_MODES,
        _EXECUTION_KEYS_WITH_IMAGES_AND_PLUGINS,
        _EXECUTION_KEYS_WITH_IMAGES_MODES_AND_PLUGINS,
    }:
        try:
            return capsule_from_json(decoded), hashlib.sha256(raw).hexdigest()
        except (TypeError, ValueError) as exc:
            raise ProjectionError("internal execution capsule is malformed") from exc
    if keys & _MODEL_KEYS and keys & _EXECUTION_KEYS:
        raise ProjectionError("capsule shape is mixed or ambiguous")
    raise ProjectionError("capsule keys do not match the closed model-facing or internal schema")


__all__ = [
    "ProjectionError",
    "load_control_capsule",
    "project_model_facing_capsule",
    "selected_checkout_facts",
]
