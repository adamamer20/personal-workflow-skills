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
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .contracts import ModelAuthority, ModelFacingCapsule, PluginRequirement
from .domain import (
    AcceptanceMode,
    ProgramApprovalGate,
    ProgramId,
    ProgramIntegrationMode,
    ProgramOutcomeKind,
    RoleId,
)

if TYPE_CHECKING:
    from .domain import ProgramGraph

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


@dataclass(frozen=True, slots=True)
class CompiledProgramNode:
    """One plan-backed node in a non-executing program projection."""

    milestone_id: str
    capsule: CompiledPlanCapsule
    dependencies: tuple[str, ...] = ()
    review_base_sha: str | None = None
    adopted_candidate_sha: str | None = None

    def __post_init__(self) -> None:
        from .domain import MilestoneId

        MilestoneId(self.milestone_id)
        if self.capsule.capsule is None:  # pragma: no cover - defensive typed guard
            raise PlanCapsuleError("program node capsule is missing")
        dependencies = tuple(self.dependencies)
        if len(dependencies) > 128 or len(set(dependencies)) != len(dependencies):
            raise PlanCapsuleError("program node dependencies are not unique")
        for dependency in dependencies:
            MilestoneId(dependency)
        object.__setattr__(self, "dependencies", dependencies)
        if (self.review_base_sha is None) != (self.adopted_candidate_sha is None):
            raise PlanCapsuleError("adopted node bindings require candidate and review base")
        for label, value in (("review base", self.review_base_sha), ("adopted candidate", self.adopted_candidate_sha)):
            if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise PlanCapsuleError(f"adopted node {label} is not a lowercase Git SHA")


@dataclass(frozen=True, slots=True)
class CompiledProgramGraph:
    """Static program graph; it never creates ledger rows or starts workers."""

    program_id: ProgramId
    plan_path: Path
    plan_revision_sha256: str
    nodes: tuple[CompiledProgramNode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, ProgramId):
            object.__setattr__(self, "program_id", ProgramId(self.program_id))
        nodes = tuple(self.nodes)
        if not nodes or len(nodes) > 128:
            raise PlanCapsuleError("program graph must contain one to 128 nodes")
        ids = tuple(node.milestone_id for node in nodes)
        if len(ids) != len(set(ids)):
            raise PlanCapsuleError("program graph milestone ids must be unique")
        known = set(ids)
        if any(dependency not in known for node in nodes for dependency in node.dependencies):
            raise PlanCapsuleError("program graph contains an unknown dependency")
        edges = {node.milestone_id: set(node.dependencies) for node in nodes}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise PlanCapsuleError("program graph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in edges[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        object.__setattr__(self, "nodes", nodes)

    def node(self, milestone_id: str) -> CompiledProgramNode:
        for node in self.nodes:
            if node.milestone_id == milestone_id:
                return node
        raise KeyError(milestone_id)

    def to_program_graph(self, state_root: Path, *, trunk_head: str | None = None) -> ProgramGraph:
        """Bind static model intent to the existing execution capsule boundary.

        ``ModelFacingCapsule`` intentionally has no runtime identity.  A
        compiled graph therefore remains a static projection until this
        method is called by the controller, which binds repository, checkout,
        route and validation facts exactly once for every node.
        """

        from .domain import MilestoneId, ProgramGraph, ProgramNodeSpec, RunId
        from .projection import project_model_facing_capsule

        selected_root = Path(state_root).resolve(strict=True)
        if trunk_head is None:
            result = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=selected_root,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise PlanCapsuleError("unable to resolve the program trunk HEAD")
            trunk_head = result.stdout.strip()
        bound_nodes = []
        for node in self.nodes:
            projected = project_model_facing_capsule(node.capsule.capsule, state_root=selected_root)
            bound_nodes.append(
                ProgramNodeSpec(
                    MilestoneId(node.milestone_id),
                    replace(
                        projected,
                        run_id=RunId(str(self.program_id)),
                        milestone_id=MilestoneId(node.milestone_id),
                    ),
                    tuple(MilestoneId(item) for item in node.dependencies),
                    node.review_base_sha,
                    node.adopted_candidate_sha,
                )
            )
        return ProgramGraph(
            self.program_id,
            self.plan_path,
            self.plan_revision_sha256,
            tuple(bound_nodes),
            trunk_head,
        )


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
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id
        in {
            "ProgramOutcomeKind",
            "ProgramIntegrationMode",
        }
    ):
        enum_type = ProgramOutcomeKind if node.value.id == "ProgramOutcomeKind" else ProgramIntegrationMode
        try:
            return enum_type[node.attr]
        except KeyError as exc:
            raise PlanCapsuleError("plan uses an unsupported program outcome value") from exc
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in {
            "RoleId",
            "ModelAuthority",
            "PluginRequirement",
            "ProgramApprovalGate",
        }:
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
            if node.func.id == "PluginRequirement":
                return PluginRequirement(*args, **keywords)
            return ProgramApprovalGate(*args, **keywords)
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


def compile_program_graph(
    path: Path,
    program_id: str,
    milestone_ids: tuple[str, ...] | list[str] | None = None,
    dependencies: dict[str, tuple[str, ...]] | None = None,
    adopted_nodes: dict[str, tuple[str, str]] | None = None,
) -> CompiledProgramGraph:
    """Compile a bounded set of canonical plan capsules into one closed DAG.

    The default is deliberately conservative and compiles only the requested
    active milestone.  A program caller may name additional plan sections and
    their dependency edges explicitly; this keeps historical plan sections
    from becoming executable merely because they remain in the Markdown file.
    """

    selected = tuple(milestone_ids) if milestone_ids is not None else (program_id,)
    if not selected:
        raise PlanCapsuleError("program graph requires at least one milestone")
    edge_map = dependencies or {}
    adopted = adopted_nodes or {}
    if any(node_id not in selected for node_id in adopted):
        raise PlanCapsuleError("adopted node binding is not part of the selected graph")
    nodes = tuple(
        CompiledProgramNode(
            milestone_id,
            compile_canonical_plan(path, milestone_id),
            tuple(edge_map.get(milestone_id, ())),
            (adopted[milestone_id][1], adopted[milestone_id][0]) if milestone_id in adopted else None,
            adopted[milestone_id][0] if milestone_id in adopted else None,
        )
        for milestone_id in selected
    )
    first = nodes[0].capsule
    if any(node.capsule.plan_revision_sha256 != first.plan_revision_sha256 for node in nodes):
        raise PlanCapsuleError("program graph nodes do not share one plan revision")
    return CompiledProgramGraph(
        ProgramId(program_id),
        first.plan_path,
        first.plan_revision_sha256,
        nodes,
    )


compile_canonical_program = compile_program_graph


__all__ = [
    "CompiledPlanCapsule",
    "CompiledProgramGraph",
    "CompiledProgramNode",
    "PlanCapsuleError",
    "compile_canonical_plan",
    "compile_canonical_program",
    "compile_program_graph",
]
