"""Closed compiler for the one active model-facing capsule in the plan.

The planning model owns the typed constructor in the canonical Markdown plan.
The controller, not the model, owns its serialization and runtime projection.
This module accepts only the literal subset used by
``ModelFacingCapsule(...)``; it never executes plan source.
"""

from __future__ import annotations

import ast
import hashlib
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import ModelAuthority, ModelFacingCapsule, PluginRequirement
from .domain import AcceptanceMode, RoleId

_MAX_PLAN_BYTES: Final[int] = 4 * 1024 * 1024
_NEXT_EXECUTION: Final[re.Pattern[str]] = re.compile(
    r"^## Next execution\s+[—-]\s*(?P<milestone>[A-Za-z0-9][A-Za-z0-9_.-]*)\s*$"
)
_FENCE: Final[re.Pattern[str]] = re.compile(r"^```(?P<language>[A-Za-z0-9_-]*)\s*$")


class PlanCapsuleError(ValueError):
    """The canonical plan does not contain one closed active capsule."""


@dataclass(frozen=True, slots=True)
class CompiledPlanCapsule:
    """The typed plan input and the revision fact bound by the controller."""

    capsule: ModelFacingCapsule
    plan_path: Path
    plan_revision_sha256: str
    source_block_sha256: str


def _safe_plan_path(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current /= component
            component_metadata = current.lstat()
            if stat.S_ISLNK(component_metadata.st_mode):
                raise PlanCapsuleError("canonical plan path cannot contain symlinks")
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PlanCapsuleError("canonical plan must be a single-link regular file")
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PlanCapsuleError("canonical plan is unavailable") from exc
    return resolved


def _literal(node: ast.AST) -> object:
    """Decode the deliberately tiny literal/typed-constructor grammar."""

    if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str | int | float | bool)):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_literal(item) for item in node.elts)
    if isinstance(node, ast.List):
        return [_literal(item) for item in node.elts]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "AcceptanceMode":
        try:
            return AcceptanceMode[node.attr]
        except KeyError as exc:
            raise PlanCapsuleError("plan uses an unsupported acceptance mode") from exc
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in {"RoleId", "ModelAuthority", "PluginRequirement"}:
            raise PlanCapsuleError("plan capsule contains an unsupported constructor")
        if any(keyword.arg is None for keyword in node.keywords):
            raise PlanCapsuleError("plan capsule does not allow expanded constructor arguments")
        args = [_literal(item) for item in node.args]
        keywords = {str(keyword.arg): _literal(keyword.value) for keyword in node.keywords}
        if len(keywords) != len(node.keywords):
            raise PlanCapsuleError("plan capsule contains duplicate constructor keywords")
        try:
            if node.func.id == "RoleId":
                return RoleId(*args, **keywords)
            if node.func.id == "ModelAuthority":
                return ModelAuthority(*args, **keywords)
            return PluginRequirement(*args, **keywords)
        except (TypeError, ValueError) as exc:
            raise PlanCapsuleError("plan capsule contains an invalid typed constructor") from exc
    raise PlanCapsuleError("plan capsule contains a non-literal expression")


def _extract_block(text: str, milestone_id: str) -> str:
    lines = text.splitlines(keepends=True)
    headings = [index for index, line in enumerate(lines) if _NEXT_EXECUTION.match(line.rstrip("\r\n"))]
    matching = [
        index for index in headings if _NEXT_EXECUTION.match(lines[index].rstrip("\r\n"))["milestone"] == milestone_id
    ]
    if len(matching) != 1:
        raise PlanCapsuleError("canonical plan must contain exactly one active milestone heading")
    start = matching[0] + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    section = lines[start:end]
    blocks: list[str] = []
    index = 0
    while index < len(section):
        match = _FENCE.match(section[index].rstrip("\r\n"))
        if match is None:
            index += 1
            continue
        if match.group("language") != "python":
            raise PlanCapsuleError("active milestone contains a non-Python code fence")
        index += 1
        content: list[str] = []
        while index < len(section) and section[index].rstrip("\r\n") != "```":
            content.append(section[index])
            index += 1
        if index == len(section):
            raise PlanCapsuleError("active milestone code fence is unterminated")
        blocks.append("".join(content))
        index += 1
    if len(blocks) != 1:
        raise PlanCapsuleError("active milestone must contain exactly one capsule block")
    return blocks[0]


def compile_canonical_plan(path: Path, milestone_id: str) -> CompiledPlanCapsule:
    """Compile one exact ``Next execution`` block without executing Python."""

    if not milestone_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", milestone_id):
        raise PlanCapsuleError("milestone id is invalid")
    plan_path = _safe_plan_path(Path(path))
    try:
        raw = plan_path.read_bytes()
    except OSError as exc:
        raise PlanCapsuleError("canonical plan cannot be read") from exc
    if len(raw) > _MAX_PLAN_BYTES:
        raise PlanCapsuleError("canonical plan exceeds its byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanCapsuleError("canonical plan is not UTF-8") from exc
    block = _extract_block(text, milestone_id)
    try:
        tree = ast.parse(block, filename=str(plan_path), mode="exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise PlanCapsuleError("active milestone capsule is not valid Python syntax") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr) or not isinstance(tree.body[0].value, ast.Call):
        raise PlanCapsuleError("active milestone must contain one capsule constructor")
    call = tree.body[0].value
    if not isinstance(call.func, ast.Name) or call.func.id != "ModelFacingCapsule":
        raise PlanCapsuleError("active milestone must call ModelFacingCapsule exactly")
    if any(keyword.arg is None for keyword in call.keywords):
        raise PlanCapsuleError("capsule constructor does not allow expanded arguments")
    args = [_literal(item) for item in call.args]
    keywords = {str(keyword.arg): _literal(keyword.value) for keyword in call.keywords}
    if len(keywords) != len(call.keywords):
        raise PlanCapsuleError("capsule constructor contains duplicate keywords")
    try:
        capsule = ModelFacingCapsule(*args, **keywords)
    except (TypeError, ValueError) as exc:
        raise PlanCapsuleError("active milestone capsule is not a valid typed contract") from exc
    return CompiledPlanCapsule(
        capsule=capsule,
        plan_path=plan_path,
        plan_revision_sha256=hashlib.sha256(raw).hexdigest(),
        source_block_sha256=hashlib.sha256(block.encode("utf-8")).hexdigest(),
    )


__all__ = ["CompiledPlanCapsule", "PlanCapsuleError", "compile_canonical_plan"]
