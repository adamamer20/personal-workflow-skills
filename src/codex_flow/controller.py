"""Agent-usable SDK-first workflow controller vertical slice."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from .app_native import AppNativeDispatchRecord, AppNativeState, AppNativeTaskAction, HostIdentity, HostReceipt
from .artifacts import write_owned_artifact, write_review_artifact
from .backends.codex_sdk import (
    LEAF_WORKER_CONFIG_OVERRIDES,
    CodexSdkAdapter,
    CodexSdkConfig,
    NativeRuntimeConfig,
)
from .config import AuthorityUnavailable, WorkflowConfig, load_workflow_config
from .contracts import (
    ModelFacingResult,
    PluginCapabilitySnapshot,
    PluginRequirement,
    format_model_facing_result_prompt,
    model_facing_result_schema,
    model_facing_result_schema_sha256,
    model_facing_review_result_schema,
    model_facing_review_result_schema_sha256,
)
from .domain import (
    AcceptanceMode,
    AuthorityPlan,
    Budget,
    ControllerCheckpoint,
    DecisionRequest,
    DecisionResponse,
    DispatchId,
    ExecutionCapsule,
    ExecutionRecord,
    ExecutionStatus,
    JsonObject,
    LifecyclePhase,
    LifecycleRecord,
    LifecycleStatus,
    LocalImageInput,
    MilestoneId,
    NativePermissionAuthority,
    NativePermissionMode,
    ProgramApprovalGate,
    ProgramGraph,
    ProgramId,
    ProgramIntegrationMode,
    ProgramOutcomeKind,
    ProgramStatus,
    ReasonCode,
    ReasoningEffort,
    RecoveryDecision,
    RecoveryOutcome,
    RenderedEvidence,
    RepairRecord,
    ReplanProposal,
    ReviewFinding,
    ReviewLifecycleResult,
    ReviewResult,
    RoleId,
    RunId,
    Schema,
    SkillInput,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    ValidationFailureCode,
    ValidationObservation,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
    canonicalize_execution_capsule,
    strict_json_loads,
    thaw_json,
    validate_local_image_inputs,
)
from .ledger import (
    HarnessRefreshBlocked,
    Ledger,
    NativeCompatibilityConflict,
    NativePermissionConflict,
    RecordNotFound,
)
from .native_profile import NativeDiscoveryCompatibilityError, NativeProfileProjection
from .plugin_capabilities import (
    PluginCapabilityError,
    _verified_skill_input,
    resolve_plugin_requirements,
    standard_codex_home,
    verify_plugin_requirements,
)
from .worktrees import WorktreeManager

if TYPE_CHECKING:
    from .plan_capsule import CompiledProgramGraph


class ControllerError(RuntimeError):
    """A controller operation cannot safely continue."""


class ResumeRequired(ControllerError):
    """A durable SDK identity exists and must be resumed explicitly."""


class UncertainPreIdentity(ControllerError):
    """SDK start failed before identity could be durably bound."""


class UncertainTurn(ControllerError):
    """An external SDK turn may have happened without a durable response."""


class UnsafeResumeCompatibilityChange(ControllerError):
    """Provider, routing, or discovery facts no longer support same-thread resume."""


class UnsafeResumePermissionChange(ControllerError):
    """Native permission authorities cannot be restricted monotonically."""


_MAX_WORKSPACE_SCAN_ENTRIES = 100_000
_MAX_WORKSPACE_SCAN_DEPTH = 64
_MAX_WORKSPACE_PATH_BYTES = 4_096
_MAX_WORKSPACE_FILE_BYTES = 536_870_912
_MAX_WORKSPACE_TOTAL_BYTES = 2_147_483_648


class Adapter(Protocol):
    def start_thread(self) -> ThreadIdentity: ...

    def resume_thread(self, thread: ThreadIdentity) -> ThreadIdentity: ...

    def run_turn(
        self,
        thread: ThreadIdentity,
        input: str | SkillInput,
        *,
        local_image_inputs: tuple[LocalImageInput, ...] = (),
        output_schema: Schema | None = None,
    ) -> TurnObservation: ...

    def close(self) -> None: ...


AdapterFactory = Callable[[CodexSdkConfig], Adapter]
ControllerFaultInjector = Callable[[str], None]


def _adapter_factory(config: CodexSdkConfig) -> Adapter:
    return CodexSdkAdapter(config)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_GIT_AUTHORITY_MAX_FILES = 4096
_GIT_AUTHORITY_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GitAuthoritySnapshot:
    """Bounded digest of the stable Git authority surface for one worktree."""

    details: JsonObject
    sha256: str


def _leaf_worker_prompt(prompt: str) -> str:
    """Remove controller-lifecycle prose from the leaf's work prompt."""

    # The model-facing projection carries a controller-owned intent appendix.
    # The leaf receives the bounded work instruction only; routing, identity,
    # successor and wake facts stay in the harness capability/ledger.
    source = prompt.split("\n\nCodex Flow model-facing intent:", 1)[0]
    result = re.sub(
        r"Remove all callback responsibility from worker prompts:.*?harness-owned\.\s*",
        "",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    if not result:
        raise ControllerError("leaf worker prompt contains no executable work instruction")
    return result


def _bounded_command(workspace: Path, arguments: tuple[str, ...]) -> bytes:
    try:
        result = subprocess.run(
            arguments,
            cwd=workspace,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError("unable to inspect bounded Git authority") from exc
    if result.returncode != 0 or len(result.stdout) > _GIT_AUTHORITY_MAX_BYTES:
        raise ControllerError("bounded Git authority command failed or exceeded its byte limit")
    return result.stdout


def _stable_git_tree_digest(
    paths: tuple[Path, ...], *, excluded_subtrees: tuple[Path, ...] = ()
) -> tuple[str, int, int]:
    """Hash selected stable metadata, excluding volatile lock files/subtrees."""

    excluded = tuple(path.resolve() for path in excluded_subtrees)

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved != root and resolved.is_relative_to(root) for root in excluded)

    def is_excluded_directory(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == root or resolved.is_relative_to(root) for root in excluded)

    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    entries: list[tuple[str, Path]] = []
    for root in paths:
        if not root.exists():
            entries.append((os.fspath(root), root))
        elif root.is_dir():
            if stat.S_ISLNK(os.lstat(root).st_mode):
                raise ControllerError("Git authority root is an indirect path")
            for current, directories, names in os.walk(root, followlinks=False):
                for name in directories:
                    metadata = os.lstat(Path(current) / name)
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        raise ControllerError("Git authority directory tree contains an indirect path")
                directories[:] = sorted(
                    name
                    for name in directories
                    if not name.endswith(".lock") and not is_excluded_directory(Path(current) / name)
                )
                for name in sorted(item for item in names if not item.endswith(".lock")):
                    path = Path(current) / name
                    if is_excluded(path):
                        continue
                    entries.append((os.fspath(path), path))
        else:
            entries.append((os.fspath(root), root))
    for label, path in sorted(entries):
        digest.update(label.encode())
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            digest.update(b"\0missing\0")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ControllerError(f"Git authority path is not a regular file: {path}")
        files += 1
        total_bytes += metadata.st_size
        if files > _GIT_AUTHORITY_MAX_FILES or total_bytes > _GIT_AUTHORITY_MAX_BYTES:
            raise ControllerError("stable Git authority surface exceeds its bounded snapshot limits")
        digest.update(path.read_bytes())
    return digest.hexdigest(), files, total_bytes


def git_authority_snapshot(workspace: Path) -> GitAuthoritySnapshot:
    """Capture refs, reflogs, index, configuration, and worktree operation state."""

    def git_path(name: str) -> Path:
        output = _bounded_command(
            workspace,
            ("git", "rev-parse", "--path-format=absolute", "--git-path", name),
        )
        return Path(os.fsdecode(output).strip()).resolve()

    git_dir = Path(
        os.fsdecode(
            _bounded_command(workspace, ("git", "rev-parse", "--path-format=absolute", "--absolute-git-dir"))
        ).strip()
    ).resolve()
    common_dir = Path(
        os.fsdecode(
            _bounded_command(workspace, ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"))
        ).strip()
    ).resolve()
    head_oid = os.fsdecode(_bounded_command(workspace, ("git", "rev-parse", "--verify", "HEAD"))).strip()
    branch = os.fsdecode(_bounded_command(workspace, ("git", "symbolic-ref", "--quiet", "--short", "HEAD"))).strip()
    refs_raw = _bounded_command(
        workspace,
        ("git", "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"),
    )
    if refs_raw and not refs_raw.endswith(b"\n"):
        raise ControllerError("Git ref inventory ended with an incomplete record")
    ref_records: list[tuple[str, str, str]] = []
    for raw_record in refs_raw.splitlines():
        fields = raw_record.split(b"\0")
        if len(fields) != 3:
            raise ControllerError("Git ref inventory contains an incomplete record")
        try:
            refname, objectname, symref = (field.decode("utf-8", errors="strict") for field in fields)
        except UnicodeDecodeError as exc:
            raise ControllerError("Git ref inventory is not valid UTF-8") from exc
        if (
            not refname.startswith("refs/")
            or refname.endswith("/")
            or any(ord(char) < 0x20 or char == "\x7f" for char in refname)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", objectname)
            or (symref and not symref.startswith("refs/"))
            or any(ord(char) < 0x20 or char == "\x7f" for char in symref)
        ):
            raise ControllerError("Git ref inventory contains an invalid record")
        ref_records.append((refname, objectname, symref))
        if len(ref_records) > _GIT_AUTHORITY_MAX_FILES:
            raise ControllerError("Git ref inventory exceeds its bounded record limit")
    ordinary_ref_records = tuple(
        sorted(record for record in ref_records if not record[0].startswith("refs/codex/turn-diffs/"))
    )
    selected = tuple(
        dict.fromkeys(
            (
                common_dir / "config",
                common_dir / "config.worktree",
                common_dir / "shallow",
                common_dir / "refs",
                common_dir / "logs" / "refs",
                git_dir / "HEAD",
                git_dir / "ORIG_HEAD",
                git_dir / "MERGE_HEAD",
                git_dir / "CHERRY_PICK_HEAD",
                git_dir / "REVERT_HEAD",
                git_dir / "BISECT_HEAD",
                git_dir / "index",
                git_dir / "config.worktree",
                git_dir / "logs" / "HEAD",
                git_dir / "rebase-apply",
                git_dir / "rebase-merge",
                git_dir / "sequencer",
            )
        )
    )
    metadata_sha256, file_count, byte_count = _stable_git_tree_digest(
        selected,
        excluded_subtrees=(
            common_dir / "refs" / "codex" / "turn-diffs",
            common_dir / "logs" / "refs" / "codex" / "turn-diffs",
        ),
    )
    refs = _canonical_json(ordinary_ref_records)
    details: JsonObject = {
        "schema": "codex-flow/git-authority/v1",
        "head_oid": head_oid,
        "branch": branch,
        "refs_sha256": _digest_bytes(refs),
        "stable_metadata_sha256": metadata_sha256,
        "stable_file_count": file_count,
        "stable_byte_count": byte_count,
        "git_dir": os.fspath(git_dir),
        "git_common_dir": os.fspath(common_dir),
        "index_path": os.fspath(git_path("index")),
    }
    return GitAuthoritySnapshot(details, _digest_bytes(_canonical_json(details)))


def capsule_json(capsule: ExecutionCapsule) -> JsonObject:
    capsule = canonicalize_execution_capsule(capsule)
    value: JsonObject = {
        "capsule_version": capsule.capsule_version,
        "run_id": str(capsule.run_id),
        "milestone_id": str(capsule.milestone_id),
        "repository_root": str(capsule.repository_root),
        "workspace_mode": capsule.workspace_mode.value,
        "workspace_path": str(capsule.workspace_path),
        "branch": capsule.branch,
        "base_sha": capsule.base_sha,
        "lane": capsule.lane,
        "mutable_paths": list(capsule.mutable_paths),
        "protected_paths": list(capsule.protected_paths),
        "validation": {
            "argv": list(capsule.validation.argv),
            "timeout_seconds": float(capsule.validation.timeout_seconds),
        },
        "model": capsule.model,
        "reasoning_effort": capsule.reasoning_effort.value,
        "prompt": capsule.prompt,
        "output_schema": thaw_json(capsule.output_schema),
        "permission_mode": capsule.permission_mode.value,
        "acceptance_modes": [mode.value for mode in capsule.acceptance_modes],
    }
    if capsule.plugin_requirements:
        value["plugin_requirements"] = [dict(item) for item in capsule.plugin_requirements]
    if capsule.local_image_paths:
        value["local_image_paths"] = list(capsule.local_image_paths)
    if (
        capsule.outcome_kind is not ProgramOutcomeKind.COMMIT
        or capsule.integration_mode is not ProgramIntegrationMode.GIT
        or capsule.approval_gates
    ):
        value.update(
            {
                "outcome_kind": capsule.outcome_kind.value,
                "integration_mode": capsule.integration_mode.value,
                "runtime_artifact_paths": list(capsule.runtime_artifact_paths),
                "runtime_artifact_max_files": capsule.runtime_artifact_max_files,
                "runtime_artifact_max_bytes": capsule.runtime_artifact_max_bytes,
                "approval_gates": [item.to_json() for item in capsule.approval_gates],
            }
        )
    return value


def capsule_from_json(value: Mapping[str, object]) -> ExecutionCapsule:
    expected = {
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
    optional = {"acceptance_modes", "plugin_requirements"}
    image_optional = {"local_image_paths"}
    outcome_optional = {
        "outcome_kind",
        "integration_mode",
        "runtime_artifact_paths",
        "runtime_artifact_max_files",
        "runtime_artifact_max_bytes",
        "approval_gates",
    }
    accepted_keys = (
        expected,
        expected | {"acceptance_modes"},
        expected | optional,
        expected | image_optional,
        expected | image_optional | {"acceptance_modes"},
        expected | image_optional | {"plugin_requirements"},
        expected | image_optional | optional,
        expected | outcome_optional,
        expected | outcome_optional | {"acceptance_modes"},
        expected | outcome_optional | {"plugin_requirements"},
        expected | outcome_optional | image_optional,
        expected | outcome_optional | image_optional | {"acceptance_modes"},
        expected | outcome_optional | image_optional | {"plugin_requirements"},
        expected | outcome_optional | image_optional | optional,
    )
    if not any(set(value) == keys for keys in accepted_keys):
        raise ValueError("capsule keys do not match the closed execution schema")

    def require_string(field: str) -> str:
        raw = value[field]
        if not isinstance(raw, str):
            raise ValueError(f"capsule {field} must be a string")
        return raw

    version = value["capsule_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("capsule capsule_version must be an integer")
    validation = value["validation"]
    if not isinstance(validation, Mapping) or set(validation) != {"argv", "timeout_seconds"}:
        raise ValueError("capsule validation must contain argv and timeout_seconds")
    argv = validation["argv"]
    mutable = value["mutable_paths"]
    protected = value["protected_paths"]
    schema = value["output_schema"]
    if (
        not isinstance(argv, list)
        or any(not isinstance(item, str) for item in argv)
        or not isinstance(mutable, list)
        or any(not isinstance(item, str) for item in mutable)
        or not isinstance(protected, list)
        or any(not isinstance(item, str) for item in protected)
    ):
        raise ValueError("capsule path and argv fields must be arrays")
    if not isinstance(schema, dict):
        raise ValueError("capsule output_schema must be an object")
    timeout = validation["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("capsule validation timeout_seconds must be a number")
    repository_root = require_string("repository_root")
    workspace_path = require_string("workspace_path")
    branch = require_string("branch")
    base_sha = require_string("base_sha")
    lane = require_string("lane")
    model = require_string("model")
    prompt = require_string("prompt")
    workspace_mode = require_string("workspace_mode")
    effort = require_string("reasoning_effort")
    permission_mode = require_string("permission_mode")
    raw_modes = value.get("acceptance_modes", [AcceptanceMode.OBJECTIVE.value])
    if not isinstance(raw_modes, list) or any(not isinstance(item, str) for item in raw_modes):
        raise ValueError("capsule acceptance_modes must be an array of strings")
    run_id = require_string("run_id")
    milestone_id = require_string("milestone_id")
    raw_requirements = value.get("plugin_requirements", [])
    if not isinstance(raw_requirements, list) or any(not isinstance(item, Mapping) for item in raw_requirements):
        raise ValueError("capsule plugin_requirements must be an array of objects")
    raw_images = value.get("local_image_paths", [])
    if not isinstance(raw_images, list) or any(not isinstance(item, str) for item in raw_images):
        raise ValueError("capsule local_image_paths must be an array of strings")
    raw_artifacts = value.get("runtime_artifact_paths", [])
    raw_gates = value.get("approval_gates", [])
    if not isinstance(raw_artifacts, list) or any(not isinstance(item, str) for item in raw_artifacts):
        raise ValueError("capsule runtime_artifact_paths must be an array of strings")
    if not isinstance(raw_gates, list) or any(not isinstance(item, Mapping) for item in raw_gates):
        raise ValueError("capsule approval_gates must be an array of objects")
    return ExecutionCapsule(
        version,
        RunId(run_id),
        MilestoneId(milestone_id),
        Path(repository_root),
        WorkspaceMode(workspace_mode),
        Path(workspace_path),
        branch,
        base_sha,
        lane,
        tuple(mutable),
        tuple(protected),
        ValidationSpec(tuple(argv), float(timeout)),
        model,
        ReasoningEffort(effort),
        prompt,
        cast(JsonObject, schema),
        NativePermissionMode(permission_mode),
        tuple(AcceptanceMode(item) for item in raw_modes),
        tuple(dict(item) for item in raw_requirements),
        tuple(raw_images),
        ProgramOutcomeKind(value.get("outcome_kind", ProgramOutcomeKind.COMMIT.value)),
        ProgramIntegrationMode(value.get("integration_mode", ProgramIntegrationMode.GIT.value)),
        tuple(raw_artifacts),
        value.get("runtime_artifact_max_files"),  # type: ignore[arg-type]
        value.get("runtime_artifact_max_bytes"),  # type: ignore[arg-type]
        tuple(ProgramApprovalGate.from_json(item) for item in raw_gates),
    )


def load_capsule(path: Path) -> tuple[ExecutionCapsule, str]:
    raw = path.read_bytes()
    try:
        decoded = strict_json_loads(raw)
    except ValueError as exc:
        raise ControllerError(f"capsule is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise ControllerError("capsule root must be an object")
    return capsule_from_json(decoded), _digest_bytes(raw)


def protected_paths_digest(root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        digest.update(relative.encode())
        listing = subprocess.run(
            ("git", "ls-files", "--cached", "-z", "--", relative),
            cwd=root,
            capture_output=True,
            check=False,
        )
        if listing.returncode != 0:
            raise ControllerError("unable to inspect protected workspace files")
        names = [os.fsdecode(item) for item in listing.stdout.split(b"\0") if item]
        if not names:
            digest.update(b"\0missing\0")
            continue
        for name in sorted(names):
            candidate = root / name
            if candidate.is_symlink():
                raise ControllerError(f"protected path contains a symlink: {candidate}")
            digest.update(name.encode())
            if not candidate.is_file():
                digest.update(b"\0missing\0")
                continue
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def protected_paths_digest_at_revision(repository: Path, revision: str, paths: tuple[str, ...]) -> str:
    """Hash protected content from the capsule base, before a worktree exists."""

    digest = hashlib.sha256()
    for relative in sorted(paths):
        digest.update(relative.encode())
        listing = subprocess.run(
            ("git", "ls-tree", "-r", "--name-only", "-z", revision, "--", relative),
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if listing.returncode != 0:
            raise ControllerError("unable to inspect protected base revision")
        names = [os.fsdecode(item) for item in listing.stdout.split(b"\0") if item]
        if not names:
            digest.update(b"\0missing\0")
            continue
        for name in sorted(names):
            content = subprocess.run(
                ("git", "show", f"{revision}:{name}"),
                cwd=repository,
                capture_output=True,
                check=False,
            )
            if content.returncode != 0:
                raise ControllerError("unable to read protected base revision")
            digest.update(name.encode())
            digest.update(content.stdout)
    return digest.hexdigest()


def _run_validation(spec: ValidationSpec, workspace: Path) -> ValidationObservation:
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            list(spec.argv),
            cwd=workspace,
            text=False,
            capture_output=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        error_code = ValidationFailureCode.TIMEOUT
    except (OSError, ValueError):
        # Do not persist platform exception text.  A stable controller-owned
        # code is sufficient to recover and diagnose an unlaunchable command.
        exit_code = -127
        stdout = b""
        stderr = b""
        error_code = ValidationFailureCode.EXECUTABLE_UNAVAILABLE
    else:
        error_code = ValidationFailureCode.NONZERO_EXIT if exit_code != 0 else None
    return ValidationObservation(
        spec.argv,
        exit_code,
        _digest_bytes(stdout),
        _digest_bytes(stderr),
        timed_out,
        time.monotonic() - started,
        error_code,
    )


class Controller:
    """Own one durable execution route from capsule to terminal result."""

    def __init__(
        self,
        state_root: Path,
        *,
        _trusted_test_adapter_factory: AdapterFactory | None = None,
        _trusted_test_native_profile: NativeProfileProjection | None = None,
        worktrees: WorktreeManager | None = None,
        fault_injector: ControllerFaultInjector | None = None,
    ) -> None:
        try:
            state_root = state_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("controller state root must have a canonical physical identity") from exc
        # Bind the controller to a real Git toplevel before Ledger can create
        # any `.codex-flow` state.  A failed/outside-root plan therefore leaves
        # no controller database residue behind.
        bound_root = self._git_toplevel(state_root)
        if bound_root != state_root.resolve():
            raise ControllerError("controller state root must equal the physical Git toplevel")
        self.state_root = bound_root
        self.state_dir = self.state_root / ".codex-flow"
        self.ledger = Ledger(self.state_dir / "workflow.db")
        # In-process adapter injection is a trusted hermetic-test seam. The
        # production constructor always resolves the sealed SDK child route.
        self._production_adapter = _trusted_test_adapter_factory is None
        self._trusted_test_native_profile = _trusted_test_native_profile
        self._adapter_factory = _trusted_test_adapter_factory or _adapter_factory
        self._worktrees = worktrees or WorktreeManager()
        self._fault_injector = fault_injector
        self._local_lock = threading.Lock()

    def close(self) -> None:
        self.ledger.close()

    def review_workflow(self, config: WorkflowConfig | None = None) -> ReviewWorkflow:
        """Return the review orchestration view over this controller's ledger."""

        return ReviewWorkflow(self.ledger, self.state_root, config)

    def register_program(
        self, program: ProgramGraph | CompiledProgramGraph, *, trunk_head: str | None = None
    ) -> ProgramStatus:
        """Compile and durably register one complete program graph.

        Registration is a plan-time operation. It creates the ordinary
        execution and milestone projections first, then binds the graph to
        the single SQLite authority; no worker or controller process starts.
        """

        from .plan_capsule import CompiledProgramGraph

        with self._mutation_lock():
            if isinstance(program, CompiledProgramGraph):
                graph = program.to_program_graph(self.state_root, trunk_head=trunk_head)
            elif isinstance(program, ProgramGraph):
                graph = program
            else:
                raise TypeError("program must be a typed compiled or executable graph")
            if any(node.capsule.approval_gates for node in graph.nodes):
                raise ControllerError("approval_capability_unavailable")
            for node in graph.nodes:
                if node.capsule.run_id != RunId(str(graph.program_id)):
                    raise ControllerError("program node capsule run identity does not match its graph")
                self._plan(node.capsule)
            return self.ledger.register_program(graph)

    def program_status(self, program_id: ProgramId | str) -> ProgramStatus:
        """Read one durable program projection without starting a scheduler."""

        return self.ledger.program_status(program_id)

    def start_program(self, program_id: ProgramId | str, *, event_key: str = "start") -> object:
        """Emit one coalesced program start event for the detached harness."""

        with self._mutation_lock():
            return self.ledger.start_program(program_id, event_key=event_key)

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    @contextmanager
    def _mutation_lock(self):
        lock_path = self.state_dir / "controller.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            with self._local_lock:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _capsule_path(self, capsule: ExecutionCapsule) -> Path:
        return self.state_dir / "capsules" / str(capsule.run_id) / f"{capsule.milestone_id}.json"

    def plan(self, capsule: ExecutionCapsule) -> ExecutionRecord:
        with self._mutation_lock():
            return self._plan(capsule)

    def _plan(self, capsule: ExecutionCapsule) -> ExecutionRecord:
        # This is the public durability boundary.  Validate and detach before
        # worktree/ledger operations so a counterfeit or caller-mutated capsule
        # cannot create even an empty run or milestone row.
        capsule = canonicalize_execution_capsule(capsule)
        self._worktrees.validate_plan(capsule)
        # The ledger and capsule are repository-owned.  A controller rooted at
        # another checkout/state directory must not be able to claim the same
        # logical dispatch or workspace.
        repository_toplevel = self._git_toplevel(capsule.repository_root)
        if repository_toplevel != capsule.repository_root:
            raise ControllerError("execution repository_root must be the physical Git toplevel")
        if self._git_toplevel(self.state_root) != self.state_root or self.state_root not in {
            repository_toplevel,
            capsule.workspace_path,
        }:
            raise ControllerError("controller state root must equal the repository or selected checkout toplevel")
        protected_before = protected_paths_digest_at_revision(
            capsule.repository_root, capsule.base_sha, capsule.protected_paths
        )
        content = _canonical_json(capsule_json(capsule))
        capsule_path = self._capsule_path(capsule)
        try:
            existing = self.ledger.get_execution(capsule.run_id, capsule.milestone_id)
        except RecordNotFound:
            pass
        else:
            if (
                existing.capsule_path != capsule_path
                or existing.capsule_sha256 != _digest_bytes(content)
                or existing.protected_before_sha256 != protected_before
            ):
                raise ControllerError("execution is already planned with different durable facts")
            durable, digest = load_capsule(capsule_path)
            if durable != capsule or digest != existing.capsule_sha256:
                raise ControllerError("existing durable capsule does not match the requested plan")
            return existing
        self.ledger.create_run(capsule.run_id)
        self.ledger.create_milestone(capsule.run_id, capsule.milestone_id)
        write_owned_artifact(
            self.state_root,
            capsule_path.relative_to(self.state_root),
            content,
            replace=False,
        )
        return self.ledger.plan_execution(
            capsule,
            capsule_path=capsule_path,
            capsule_sha256=_digest_bytes(content),
            protected_before_sha256=protected_before,
        )

    def _load_durable_capsule(self, record: ExecutionRecord) -> ExecutionCapsule:
        capsule, digest = load_capsule(record.capsule_path)
        if digest != record.capsule_sha256 or (capsule.run_id, capsule.milestone_id) != (
            record.run_id,
            record.milestone_id,
        ):
            raise ControllerError("durable capsule identity or digest changed")
        return capsule

    def enqueue(
        self,
        capsule: ExecutionCapsule,
        *,
        role: RoleId | str = "executor",
        generation: int = 1,
        backend: str = "sdk_headless",
        action_json: str | None = None,
        plan_path: Path | str = "internal",
        plan_revision_sha256: str | None = None,
        projection_sha256: str | None = None,
        checkpoint_seconds: float = 1800.0,
    ) -> dict[str, object]:
        """Durably claim and queue a dispatch for the detached harness."""

        role_value = role if isinstance(role, RoleId) else RoleId(str(role))
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("dispatch generation must be a positive integer")
        source_thread_id = os.environ.get("CODEX_THREAD_ID") or None
        if source_thread_id is not None:
            try:
                source_thread_id = ThreadIdentity(source_thread_id).id
            except ValueError as exc:
                raise ControllerError("CODEX_THREAD_ID is not a valid source controller identity") from exc
        with self._mutation_lock():
            if self.ledger.harness_refresh_fenced():
                raise HarnessRefreshBlocked("harness refresh fence is active")
            planned = self._plan(capsule)
            existing_dispatch = None
            try:
                existing_dispatch = self.ledger.get_dispatch(
                    DispatchId.from_parts(planned.run_id, planned.milestone_id, role_value, generation)
                )
            except RecordNotFound:
                pass
            profile = (
                NativeProfileProjection.load(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve())
                if self._production_adapter
                else self._trusted_test_native_profile
            )
            if profile is None:
                # Trusted hermetic tests do not have a native profile to
                # inherit.  Keep that low-level seam conservative; production
                # enqueue always binds the actual native profile above.
                effective_permission = NativePermissionAuthority(
                    capsule.permission_mode,
                    "read-only",
                    "never",
                )
                native_profile_sha256 = None
                native_compatibility_sha256 = None
            else:
                if backend == "sdk_headless":
                    profile.verify_worker_sources()
                else:
                    profile.verify_sources()
                effective_permission = profile.effective_authority(capsule.permission_mode)
                native_profile_sha256 = profile.profile_sha256
                native_compatibility_sha256 = (
                    profile.worker_compatibility_sha256 if backend == "sdk_headless" else profile.compatibility_sha256
                )
            model = capsule.model
            reasoning_effort = capsule.reasoning_effort
            if role_value != RoleId("executor"):
                try:
                    reviewer_route = load_workflow_config(self.state_root / "workflow.toml").route(role_value)
                except (AuthorityUnavailable, KeyError, ValueError) as exc:
                    raise ControllerError(f"reviewer route is unavailable for {role_value}") from exc
                model = reviewer_route.model
                reasoning_effort = reviewer_route.reasoning_effort
            try:
                plugin_requirements = tuple(PluginRequirement.from_json(item) for item in capsule.plugin_requirements)
                if plugin_requirements and backend != "sdk_headless":
                    raise PluginCapabilityError("required plugins are unsupported by the App-native route")
                plugin_snapshots = resolve_plugin_requirements(
                    plugin_requirements,
                    profile.source_home if profile is not None else standard_codex_home(),
                )
            except PluginCapabilityError as exc:
                raise ControllerError(f"plugin capability is unavailable: {exc}") from exc
            route = {
                "role": str(role_value),
                "model": model,
                "reasoning_effort": reasoning_effort.value,
                "effective_permission": effective_permission.facts,
                "native_profile_sha256": native_profile_sha256,
                "native_compatibility_sha256": native_compatibility_sha256,
                "plugin_requirements": [item.to_json() for item in plugin_requirements],
                "plugin_capabilities": [
                    {**item.to_json(), "capability_sha256": item.capability_digest} for item in plugin_snapshots
                ],
                # This policy is an immutable queue fact consumed by the
                # detached worker before SDK thread creation.  App-native
                # creation has no equivalent per-task override in the donor
                # API and is therefore explicitly marked unsupported rather
                # than pretending prompt prose enforces leaf topology.
                "leaf_worker_policy": (
                    {
                        "agents.enabled": False,
                        "features.multi_agent": False,
                        "config_overrides": list(LEAF_WORKER_CONFIG_OVERRIDES),
                    }
                    if backend == "sdk_headless"
                    else {"status": "unsupported_app_native_per_task_override"}
                ),
            }
            queue_projection = capsule_json(capsule)
            result_schema_version = 2 if capsule.outcome_kind is ProgramOutcomeKind.RUNTIME_EVIDENCE else 1
            if (
                capsule.outcome_kind is ProgramOutcomeKind.RUNTIME_EVIDENCE
                and capsule.output_schema != model_facing_result_schema(2)
            ):
                raise ControllerError("runtime evidence dispatch requires the schema-2 result contract")
            result_contract_sha256 = model_facing_result_schema_sha256(result_schema_version)
            if role_value != RoleId("executor"):
                # Reviewers receive a separate closed result contract and a
                # controller-authored candidate binding.  Their queue route
                # remains read-only and cannot create a second executor.
                queue_projection["output_schema"] = model_facing_review_result_schema()
                candidate_context = action_json or "{}"
                review_schema_json = json.dumps(
                    model_facing_review_result_schema(), sort_keys=True, separators=(",", ":")
                )
                queue_projection["prompt"] = (
                    "Read-only acceptance review. Inspect only the exact candidate and declared acceptance "
                    "authority named by the controller. Do not edit files, run mutable commands, or create "
                    "workflow state. Return exactly one JSON reviewer result object and no prose.\n"
                    f"controller_candidate={candidate_context}\n"
                    f"review_schema={review_schema_json}"
                )
                result_contract_sha256 = model_facing_review_result_schema_sha256()
            else:
                queue_projection["prompt"] = _leaf_worker_prompt(str(queue_projection["prompt"]))
            projected = _canonical_json(queue_projection).decode("utf-8")
            if existing_dispatch is None:
                # Bind all native/profile and generated projection facts before
                # creating the execution dispatch authority.
                if role_value == RoleId("executor"):
                    if generation == 1:
                        self.ledger.claim_dispatch(planned.run_id, planned.milestone_id, role_value, generation)
                    else:
                        self.ledger.claim_program_repair_dispatch(
                            planned.run_id, planned.milestone_id, generation=generation
                        )
                else:
                    self.ledger.claim_program_review_dispatch(
                        planned.run_id, planned.milestone_id, role_value, generation
                    )
            return self.ledger.enqueue_dispatch(
                DispatchId.from_parts(planned.run_id, planned.milestone_id, role_value, generation),
                backend=backend,
                capsule_json=projected,
                route_json=json.dumps(route, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                workspace_path=capsule.workspace_path,
                result_contract_sha256=result_contract_sha256,
                action_json=action_json,
                plan_path=plan_path,
                plan_revision_sha256=plan_revision_sha256,
                projection_sha256=projection_sha256 or _digest_bytes(projected.encode("utf-8")),
                source_thread_id=source_thread_id,
                permission_mode=capsule.permission_mode,
                native_profile_sha256=native_profile_sha256,
                native_compatibility_sha256=native_compatibility_sha256,
                effective_permission=effective_permission,
                checkpoint_seconds=checkpoint_seconds,
            )

    def _assert_protected_clean(self, capsule: ExecutionCapsule) -> None:
        if not capsule.protected_paths:
            return
        result = subprocess.run(
            ("git", "status", "--porcelain", "--", *capsule.protected_paths),
            cwd=capsule.workspace_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ControllerError(f"unable to inspect protected paths: {result.stderr.strip()}")
        if result.stdout.strip():
            raise ControllerError("protected paths are dirty before execution")

    @staticmethod
    def _git_toplevel(path: Path) -> Path:
        result = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=path,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ControllerError("execution repository is not a Git checkout")
        return Path(result.stdout.strip()).resolve()

    @staticmethod
    def _git_output(workspace: Path, *arguments: str) -> str:
        result = subprocess.run(("git", *arguments), cwd=workspace, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise ControllerError(f"unable to inspect Git workspace: {' '.join(arguments)}")
        return result.stdout.strip()

    @staticmethod
    def _git_output_bytes(workspace: Path, *arguments: str) -> bytes:
        result = subprocess.run(("git", *arguments), cwd=workspace, capture_output=True, check=False)
        if result.returncode != 0:
            raise ControllerError(f"unable to inspect Git workspace: {' '.join(arguments)}")
        return result.stdout

    @staticmethod
    def _git_workspace_filter(workspace: Path) -> tuple[frozenset[str], frozenset[str]]:
        """Return tracked paths and Git-ignored untracked paths.

        Git is the authority for ordinary source scope.  The ``--directory``
        form intentionally collapses ignored trees (``.venv/``, ``node_modules/``
        and build output) so topology scans can prune them without traversing
        their contents.  Tracked paths are kept separately because a tracked
        path remains in scope even when an ignore rule also matches it.
        """

        tracked = subprocess.run(
            ("git", "ls-files", "--cached", "-z"),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        ignored = subprocess.run(
            ("git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if tracked.returncode != 0 or ignored.returncode != 0:
            raise ControllerError("unable to inspect Git ignored-artifact policy")
        tracked_paths = frozenset(os.fsdecode(item) for item in tracked.stdout.split(b"\0") if item)
        ignored_paths = frozenset(os.fsdecode(item).rstrip("/") for item in ignored.stdout.split(b"\0") if item)
        return tracked_paths, ignored_paths

    @staticmethod
    def _path_is_ignored(
        workspace: Path,
        relative: str,
        tracked_paths: frozenset[str],
        ignored_paths: frozenset[str],
    ) -> bool:
        """Apply Git ignore rules to one path without hiding tracked descendants."""

        candidate = Path(relative).as_posix()
        if any(tracked == candidate or tracked.startswith(candidate + "/") for tracked in tracked_paths):
            return False
        if any(candidate == ignored or Path(ignored) in Path(candidate).parents for ignored in ignored_paths):
            return True
        result = subprocess.run(
            ("git", "check-ignore", "--no-index", "--quiet", "--", candidate),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise ControllerError("unable to inspect Git ignored-artifact policy")

    def _native_runtime(
        self, capsule: ExecutionCapsule
    ) -> tuple[NativeRuntimeConfig | None, str, str, NativePermissionAuthority]:
        runtime_root = self.state_dir / "sdk-runtime" / str(capsule.run_id) / str(capsule.milestone_id)
        global_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
        native_profile = (
            NativeProfileProjection.load(global_home) if self._production_adapter else self._trusted_test_native_profile
        )
        runtime = NativeRuntimeConfig(runtime_root / "home", native_profile) if native_profile is not None else None
        effective_permission = (
            native_profile.effective_authority(capsule.permission_mode)
            if native_profile is not None
            else NativePermissionAuthority(NativePermissionMode.INHERIT_NATIVE, "workspace-write", "never")
        )
        facts: JsonObject = {
            "schema": "codex-flow/native-runtime-profile/v1",
            "workspace": os.fspath(capsule.workspace_path),
            "effective_permissions": (
                effective_permission.facts if native_profile is not None else {"test_seam": True}
            ),
            "runtime_home": os.fspath(runtime.runtime_home) if runtime is not None else None,
            "native_profile": native_profile.sanitized_facts if native_profile is not None else {"test_seam": True},
            "transport": "openai-codex-sdk-app-server",
        }
        compatibility_facts: JsonObject = {
            "schema": "codex-flow/native-runtime-compatibility/v1",
            "workspace": os.fspath(capsule.workspace_path),
            "native_compatibility_sha256": (
                native_profile.compatibility_sha256 if native_profile is not None else "test-seam"
            ),
            "transport": "openai-codex-sdk-app-server",
        }
        return (
            runtime,
            _digest_bytes(_canonical_json(facts)),
            _digest_bytes(_canonical_json(compatibility_facts)),
            effective_permission,
        )

    def _committed_workspace_paths(self, capsule: ExecutionCapsule, baseline_head: str) -> frozenset[str]:
        """Return every path touched by every commit after the milestone baseline.

        Comparing only the final tree to ``base_sha`` misses an out-of-scope
        commit that is subsequently reverted.  The commit walk is therefore a
        separate integrity fact from the final-tree diff.
        """

        commits = self._git_output_bytes(
            capsule.workspace_path,
            "rev-list",
            "--reverse",
            "--ancestry-path",
            f"{baseline_head}..HEAD",
        )
        paths: set[str] = set()
        for raw_commit in commits.splitlines():
            commit = os.fsdecode(raw_commit)
            parents = self._git_output(capsule.workspace_path, "rev-list", "--parents", "-n", "1", commit).split()
            options = ("--no-commit-id", "--name-only", "-r", "-z")
            if len(parents) > 2:
                options = (*options[:-1], "-m", options[-1])
            changed = self._git_output_bytes(capsule.workspace_path, "diff-tree", *options, commit)
            paths.update(os.fsdecode(item) for item in changed.split(b"\0") if item)
        return frozenset(paths)

    def _assert_workspace_history(
        self,
        capsule: ExecutionCapsule,
        baseline_head: str,
        mutable_paths: tuple[str, ...] | None = None,
    ) -> None:
        branch = self._git_output(capsule.workspace_path, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != capsule.branch:
            raise ControllerError("workspace branch changed during execution")
        ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", capsule.base_sha, "HEAD"),
            cwd=capsule.workspace_path,
            capture_output=True,
            check=False,
        )
        if ancestor.returncode != 0:
            raise ControllerError("workspace history no longer descends from the capsule base")
        baseline_ancestor = subprocess.run(
            ("git", "merge-base", "--is-ancestor", baseline_head, "HEAD"),
            cwd=capsule.workspace_path,
            capture_output=True,
            check=False,
        )
        if baseline_ancestor.returncode != 0:
            raise ControllerError("workspace history no longer descends from the milestone baseline")
        paths = self._committed_workspace_paths(capsule, baseline_head)
        owned_roots = capsule.mutable_paths if mutable_paths is None else mutable_paths
        outside = sorted(path for path in paths if not self._path_is_owned(path, owned_roots))
        if outside:
            raise ControllerError("committed workspace changes outside mutable paths: " + ", ".join(outside))

    def _workspace_changes(self, workspace: Path, revision: str = "HEAD") -> frozenset[str]:
        tracked = subprocess.run(
            ("git", "diff", "--name-only", "-z", revision, "--"),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        untracked = subprocess.run(
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if tracked.returncode != 0 or untracked.returncode != 0:
            raise ControllerError("unable to inspect workspace mutation scope")
        return frozenset(
            path
            for path in (
                *(os.fsdecode(item) for item in tracked.stdout.split(b"\0") if item),
                *(os.fsdecode(item) for item in untracked.stdout.split(b"\0") if item),
            )
            if path
        )

    def _revision_directories(self, capsule: ExecutionCapsule, revision: str) -> frozenset[str]:
        raw = self._git_output_bytes(capsule.workspace_path, "ls-tree", "-r", "--name-only", "-z", revision)
        directories: set[str] = set()
        entry_count = 0
        for item in raw.split(b"\0"):
            if not item:
                continue
            entry_count += 1
            if entry_count > _MAX_WORKSPACE_SCAN_ENTRIES:
                raise ControllerError("workspace revision topology exceeds the entry limit")
            if len(item) > _MAX_WORKSPACE_PATH_BYTES:
                raise ControllerError("workspace revision topology contains an overlong path")
            path = Path(os.fsdecode(item))
            if len(path.parts) > _MAX_WORKSPACE_SCAN_DEPTH:
                raise ControllerError("workspace revision topology exceeds the depth limit")
            for parent in path.parents:
                relative = parent.as_posix()
                if relative == ".":
                    break
                directories.add(relative)
                if len(directories) > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise ControllerError("workspace directory topology exceeds the entry limit")
        return frozenset(directories)

    def _workspace_directories(self, capsule: ExecutionCapsule) -> frozenset[str]:
        """Capture bounded repository directory topology without following links."""

        workspace = capsule.workspace_path
        tracked_paths, ignored_paths = self._git_workspace_filter(workspace)
        directories: set[str] = set()
        entries_seen = 0
        pending: list[tuple[Path, int]] = [(workspace, 0)]
        while pending:
            current, depth = pending.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name), reverse=True)
            except OSError as exc:
                raise ControllerError("unable to inspect workspace directory topology") from exc
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(workspace).as_posix()
                if len(os.fsencode(relative)) > _MAX_WORKSPACE_PATH_BYTES:
                    raise ControllerError("workspace directory topology contains an overlong path")
                if self._path_is_ignored(workspace, relative, tracked_paths, ignored_paths):
                    continue
                entries_seen += 1
                if entries_seen > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise ControllerError("workspace directory topology exceeds the entry limit")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ControllerError("workspace directory topology changed during capture") from exc
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                if relative == ".git" or Path(".git") in Path(relative).parents:
                    continue
                if self._is_controller_path(workspace, relative):
                    continue
                child_depth = depth + 1
                if child_depth > _MAX_WORKSPACE_SCAN_DEPTH:
                    raise ControllerError("workspace directory topology exceeds the depth limit")
                directories.add(relative)
                pending.append((path, child_depth))
        return frozenset(directories)

    def _directory_topology_changes(self, capsule: ExecutionCapsule, revision: str) -> tuple[tuple[str, str], ...]:
        current = self._workspace_directories(capsule)
        expected = self._revision_directories(capsule, revision)
        return tuple(
            (relative, self._directory_topology_signature(capsule.workspace_path / relative))
            for relative in sorted(current ^ expected)
        )

    @staticmethod
    def _directory_topology_signature(path: Path) -> str:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISDIR(metadata.st_mode):
            raise ControllerError(f"workspace directory topology changed type during capture: {path}")
        payload = f"topology:{stat.S_IMODE(metadata.st_mode):04o}".encode()
        return f"directory:{_digest_bytes(payload)}"

    def _is_controller_path(self, workspace: Path, path: str) -> bool:
        if workspace.resolve() != self.state_root:
            return False
        candidate = Path(path)
        internal = Path(".codex-flow")
        return candidate == internal or internal in candidate.parents

    def _path_signature(
        self,
        path: Path,
        *,
        workspace: Path | None = None,
        tracked_paths: frozenset[str] | None = None,
        ignored_paths: frozenset[str] | None = None,
    ) -> str:
        workspace = path if workspace is None else workspace
        if tracked_paths is None or ignored_paths is None:
            tracked_paths, ignored_paths = self._git_workspace_filter(workspace)
        try:
            root_relative = path.relative_to(workspace).as_posix()
        except ValueError:
            root_relative = ""
        if root_relative and self._path_is_ignored(workspace, root_relative, tracked_paths, ignored_paths):
            return "ignored"
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return "missing"
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ControllerError(f"workspace baseline contains a multiply-linked file: {path}")
            if metadata.st_size > _MAX_WORKSPACE_FILE_BYTES:
                raise ControllerError(f"workspace baseline contains an oversized file: {path}")
            payload = f"{stat.S_IMODE(metadata.st_mode):04o}\0".encode() + path.read_bytes()
            return f"file:{_digest_bytes(payload)}"
        if stat.S_ISDIR(metadata.st_mode):
            entries: list[str] = [f"root:{stat.S_IMODE(metadata.st_mode):04o}"]
            entry_count = 0
            total_bytes = 0
            for current, directories, files in os.walk(path, followlinks=False):
                directories.sort()
                files.sort()
                directories[:] = [
                    name
                    for name in directories
                    if not self._path_is_ignored(
                        workspace,
                        (Path(current) / name).relative_to(workspace).as_posix(),
                        tracked_paths,
                        ignored_paths,
                    )
                ]
                files[:] = [
                    name
                    for name in files
                    if not self._path_is_ignored(
                        workspace,
                        (Path(current) / name).relative_to(workspace).as_posix(),
                        tracked_paths,
                        ignored_paths,
                    )
                ]
                for name in (*directories, *files):
                    entry_count += 1
                    if entry_count > _MAX_WORKSPACE_SCAN_ENTRIES:
                        raise ControllerError(f"workspace baseline tree exceeds the entry limit: {path}")
                    child = Path(current) / name
                    item = os.lstat(child)
                    if stat.S_ISLNK(item.st_mode):
                        raise ControllerError(f"workspace baseline contains a symlink: {child}")
                    relative = child.relative_to(path).as_posix()
                    if len(os.fsencode(relative)) > _MAX_WORKSPACE_PATH_BYTES:
                        raise ControllerError(f"workspace baseline tree contains an overlong path: {path}")
                    if len(Path(relative).parts) > _MAX_WORKSPACE_SCAN_DEPTH:
                        raise ControllerError(f"workspace baseline tree exceeds the depth limit: {path}")
                    if stat.S_ISDIR(item.st_mode):
                        entries.append(f"d:{stat.S_IMODE(item.st_mode):04o}:{relative}")
                    elif stat.S_ISREG(item.st_mode):
                        if item.st_nlink != 1:
                            raise ControllerError(f"workspace baseline contains a multiply-linked file: {child}")
                        if item.st_size > _MAX_WORKSPACE_FILE_BYTES:
                            raise ControllerError(f"workspace baseline contains an oversized file: {child}")
                        total_bytes += item.st_size
                        if total_bytes > _MAX_WORKSPACE_TOTAL_BYTES:
                            raise ControllerError(f"workspace baseline tree exceeds the file-byte limit: {path}")
                        entries.append(
                            f"f:{stat.S_IMODE(item.st_mode):04o}:{relative}:{_digest_bytes(child.read_bytes())}"
                        )
                    else:
                        raise ControllerError(f"workspace baseline contains an unsupported object: {child}")
            return f"directory:{_digest_bytes(chr(10).join(entries).encode())}"
        raise ControllerError(f"workspace baseline contains an unsupported object: {path}")

    def _workspace_baseline_snapshot(
        self, capsule: ExecutionCapsule, baseline_head: str
    ) -> tuple[tuple[str, str], ...]:
        changes = self._workspace_changes(capsule.workspace_path, baseline_head)
        workspace_device = os.stat(capsule.workspace_path).st_dev
        facts: list[tuple[str, str]] = []
        for relative in sorted(changes):
            if self._is_controller_path(capsule.workspace_path, relative):
                continue
            self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            facts.append(
                (
                    relative,
                    self._path_signature(
                        capsule.workspace_path / relative,
                        workspace=capsule.workspace_path,
                    ),
                )
            )
        by_path = dict(facts)
        for relative, signature in self._directory_topology_changes(capsule, baseline_head):
            by_path.setdefault(relative, signature)
        return tuple(sorted(by_path.items()))

    def _owned_workspace_snapshot(self, capsule: ExecutionCapsule) -> tuple[tuple[str, str], ...]:
        workspace_device = os.stat(capsule.workspace_path).st_dev
        tracked_paths, ignored_paths = self._git_workspace_filter(capsule.workspace_path)
        facts: list[tuple[str, str]] = []
        for relative in sorted(capsule.mutable_paths):
            if self._path_is_ignored(capsule.workspace_path, relative, tracked_paths, ignored_paths):
                continue
            self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            self._assert_safe_tree(
                capsule.workspace_path / relative,
                workspace_device,
                workspace=capsule.workspace_path,
                tracked_paths=tracked_paths,
                ignored_paths=ignored_paths,
            )
            facts.append(
                (
                    relative,
                    self._path_signature(
                        capsule.workspace_path / relative,
                        workspace=capsule.workspace_path,
                        tracked_paths=tracked_paths,
                        ignored_paths=ignored_paths,
                    ),
                )
            )
        return tuple(facts)

    def _assert_predecessor_terminal_authority(
        self,
        capsule: ExecutionCapsule,
        predecessors: tuple[ExecutionRecord, ...],
    ) -> None:
        if not predecessors:
            return
        current_head = self._git_output(capsule.workspace_path, "rev-parse", "HEAD")
        current_git = git_authority_snapshot(capsule.workspace_path).sha256
        for predecessor in predecessors:
            predecessor_capsule = self._load_durable_capsule(predecessor)
            app_native = self.ledger.get_app_native_for_execution(predecessor.run_id, predecessor.milestone_id)
            if app_native is not None:
                if (
                    app_native.state is not AppNativeState.COMPLETED
                    or app_native.workspace_terminal_head_sha is None
                    or app_native.workspace_terminal is None
                    or app_native.git_authority_after_sha256 is None
                ):
                    raise ControllerError(
                        "App-native predecessor terminal authority is unavailable; explicit reconciliation is required"
                    )
                if current_head != app_native.workspace_terminal_head_sha:
                    raise ControllerError("App-native predecessor terminal Git HEAD changed before successor start")
                if current_git != app_native.git_authority_after_sha256:
                    raise ControllerError("App-native predecessor Git authority changed before successor start")
                if self._owned_workspace_snapshot(predecessor_capsule) != app_native.workspace_terminal:
                    raise ControllerError("App-native predecessor-owned content changed before successor start")
                continue
            integrity = self.ledger.get_execution_integrity(predecessor.run_id, predecessor.milestone_id)
            if (
                integrity.workspace_terminal_head_sha is None
                or integrity.workspace_terminal is None
                or integrity.git_authority_after_sha256 is None
            ):
                raise ControllerError(
                    "predecessor terminal workspace authority is unavailable; explicit reconciliation is required"
                )
            if current_head != integrity.workspace_terminal_head_sha:
                raise ControllerError("predecessor terminal Git HEAD changed before successor start")
            if current_git != integrity.git_authority_after_sha256:
                raise ControllerError("predecessor terminal Git authority changed before successor start")
            if self._owned_workspace_snapshot(predecessor_capsule) != integrity.workspace_terminal:
                raise ControllerError("predecessor-owned terminal workspace content changed before successor start")

    def _assert_safe_tree(
        self,
        root: Path,
        workspace_device: int,
        *,
        workspace: Path | None = None,
        tracked_paths: frozenset[str] | None = None,
        ignored_paths: frozenset[str] | None = None,
    ) -> None:
        """Reject symlink traversal and multiply-linked files in mutable roots."""

        workspace = root if workspace is None else workspace
        if tracked_paths is None or ignored_paths is None:
            tracked_paths, ignored_paths = self._git_workspace_filter(workspace)
        try:
            root_relative = root.relative_to(workspace).as_posix()
        except ValueError:
            root_relative = ""
        if root_relative and self._path_is_ignored(workspace, root_relative, tracked_paths, ignored_paths):
            return
        pending: list[tuple[Path, int]] = [(root, 0)]
        entries_seen = 0
        while pending:
            current, depth = pending.pop()
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                if current == root:
                    return
                raise ControllerError(f"mutable path changed during inspection: {root}") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise ControllerError(f"mutable path is a symlink: {current}")
            if metadata.st_dev != workspace_device:
                raise ControllerError(f"mutable path is outside the workspace device: {current}")
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ControllerError(f"mutable file has external hardlinks: {current}")
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise ControllerError(f"mutable path is not a regular file or directory: {current}")
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name), reverse=True)
            except OSError as exc:
                raise ControllerError(f"unable to inspect mutable path: {root}") from exc
            for entry in entries:
                child = Path(entry.path)
                child_relative = child.relative_to(workspace).as_posix()
                if self._path_is_ignored(workspace, child_relative, tracked_paths, ignored_paths):
                    continue
                entries_seen += 1
                if entries_seen > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise ControllerError(f"mutable path exceeds the entry limit: {root}")
                relative = child.relative_to(root)
                if len(os.fsencode(relative.as_posix())) > _MAX_WORKSPACE_PATH_BYTES:
                    raise ControllerError(f"mutable path contains an overlong entry: {root}")
                child_depth = depth + 1
                if child_depth > _MAX_WORKSPACE_SCAN_DEPTH:
                    raise ControllerError(f"mutable path exceeds the depth limit: {root}")
                pending.append((child, child_depth))

    @staticmethod
    def _assert_safe_changed_path(workspace: Path, relative: str, workspace_device: int) -> None:
        candidate = Path(relative)
        current = workspace
        parts = candidate.parts
        for index, part in enumerate(parts):
            current /= part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(metadata.st_mode):
                raise ControllerError(f"workspace mutation traverses a symlink: {relative}")
            if metadata.st_dev != workspace_device:
                raise ControllerError(f"workspace mutation leaves the repository device: {relative}")
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ControllerError(f"workspace mutation traverses a non-directory: {relative}")
            if index == len(parts) - 1 and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise ControllerError(f"workspace mutation uses an external hardlink: {relative}")

    @staticmethod
    def _controller_tree_snapshot(root: Path) -> dict[str, str]:
        internal = root / ".codex-flow"
        try:
            metadata = os.lstat(internal)
        except FileNotFoundError:
            return {}
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ControllerError("controller state root is not a real directory")
        snapshot: dict[str, str] = {}
        for current, directories, files in os.walk(internal, followlinks=False):
            if Path(current) == internal:
                directories[:] = [name for name in directories if name != "sdk-runtime"]
            for name in sorted((*directories, *files)):
                path = Path(current) / name
                relative = str(path.relative_to(root))
                item = os.lstat(path)
                if stat.S_ISLNK(item.st_mode):
                    raise ControllerError("controller state contains a symlink")
                if stat.S_ISDIR(item.st_mode):
                    snapshot[relative] = "directory"
                elif stat.S_ISREG(item.st_mode):
                    if item.st_nlink != 1:
                        raise ControllerError("controller state contains a multiply-linked file")
                    snapshot[relative] = _digest_bytes(path.read_bytes())
                else:
                    raise ControllerError("controller state contains an unsupported filesystem object")
        return snapshot

    @staticmethod
    def _path_is_owned(path: str, roots: tuple[str, ...]) -> bool:
        candidate = Path(path)
        return any(candidate == Path(root) or Path(root) in candidate.parents for root in roots)

    def _assert_mutation_scope(
        self,
        capsule: ExecutionCapsule,
        baseline_head: str,
        baseline: tuple[tuple[str, str], ...],
        mutable_paths: tuple[str, ...] | None = None,
    ) -> None:
        current = dict(self._workspace_baseline_snapshot(capsule, baseline_head))
        prior = dict(baseline)
        changes = frozenset(path for path in current.keys() | prior.keys() if current.get(path) != prior.get(path))
        workspace_device = os.stat(capsule.workspace_path).st_dev
        tracked_paths, ignored_paths = self._git_workspace_filter(capsule.workspace_path)
        for relative in capsule.mutable_paths:
            if not self._path_is_ignored(capsule.workspace_path, relative, tracked_paths, ignored_paths):
                self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            self._assert_safe_tree(
                capsule.workspace_path / relative,
                workspace_device,
                workspace=capsule.workspace_path,
                tracked_paths=tracked_paths,
                ignored_paths=ignored_paths,
            )
        for relative in changes:
            if not self._is_controller_path(capsule.workspace_path, relative):
                self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
        owned_roots = capsule.mutable_paths if mutable_paths is None else mutable_paths
        outside = sorted(
            path
            for path in changes
            if not self._is_controller_path(capsule.workspace_path, path) and not self._path_is_owned(path, owned_roots)
        )
        if outside:
            raise ControllerError(f"workspace contains changes outside mutable paths: {', '.join(outside)}")

    def _stable_workspace_authority(
        self,
        capsule: ExecutionCapsule,
        baseline_head: str,
    ) -> tuple[tuple[tuple[str, str], ...], str, str]:
        """Capture one bounded workspace authority twice and reject scan races."""

        captures: list[tuple[tuple[tuple[str, str], ...], str, str]] = []
        for _ in range(2):
            self._worktrees.select(capsule)
            self._assert_protected_clean(capsule)
            if self._git_output(capsule.workspace_path, "rev-parse", "HEAD") != baseline_head:
                raise ControllerError("workspace HEAD changed from its durable baseline before external call")
            self._assert_workspace_history(capsule, baseline_head)
            git_before = git_authority_snapshot(capsule.workspace_path).sha256
            baseline = self._workspace_baseline_snapshot(capsule, baseline_head)
            protected = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
            git_after = git_authority_snapshot(capsule.workspace_path).sha256
            if git_before != git_after:
                raise ControllerError("Git authority changed during pre-external authorization")
            captures.append((baseline, git_after, protected))
        if captures[0] != captures[1]:
            raise ControllerError("workspace changed during pre-external authorization")
        return captures[0]

    def _authorize_external_call(
        self, capsule: ExecutionCapsule, record: ExecutionRecord
    ) -> tuple[tuple[PluginRequirement, ...], tuple[PluginCapabilitySnapshot, ...], Path]:
        """Exactly revalidate every durable authority immediately before an external boundary."""

        plugin_requirements = tuple(PluginRequirement.from_json(item) for item in capsule.plugin_requirements)
        snapshots: tuple[PluginCapabilitySnapshot, ...] = ()
        profile = self._trusted_test_native_profile if not self._production_adapter else None
        home = profile.source_home if profile is not None else standard_codex_home()
        try:
            if plugin_requirements:
                snapshots = verify_plugin_requirements(plugin_requirements, home)
                try:
                    queue = self.ledger.queue_dispatch(
                        DispatchId.from_parts(record.run_id, record.milestone_id, "executor", 1)
                    )
                except RecordNotFound:
                    queue = None
                if queue is None:
                    route = None
                else:
                    route_raw = queue.get("route_json")
                    if not isinstance(route_raw, str):
                        raise ControllerError("durable plugin capability route is missing")
                    route = strict_json_loads(route_raw, max_bytes=1_048_576)
                    if not isinstance(route, Mapping):
                        raise ControllerError("durable plugin capability route is malformed")
                expected_requirements = [item.to_json() for item in plugin_requirements]
                expected_capabilities = [
                    {**item.to_json(), "capability_sha256": item.capability_digest} for item in snapshots
                ]
                if route is not None and route.get("plugin_requirements") != expected_requirements:
                    raise ControllerError("durable plugin requirement binding changed before SDK start")
                if route is not None and route.get("plugin_capabilities") != expected_capabilities:
                    raise ControllerError("durable plugin capability digest changed before SDK start")
        except PluginCapabilityError as exc:
            raise ControllerError(f"plugin capability changed before SDK start: {exc}") from exc

        current = self.ledger.get_execution(record.run_id, record.milestone_id)
        if current.workspace_path != capsule.workspace_path:
            raise ControllerError("durable execution workspace changed before external call")
        lease = self.ledger.get_workspace_lease(capsule.workspace_path)
        if (
            lease.workspace_path,
            lease.repository_root,
            lease.mode,
            lease.branch,
            lease.base_sha,
            lease.lane,
            lease.owner_run_id,
        ) != (
            capsule.workspace_path,
            capsule.repository_root,
            capsule.workspace_mode,
            capsule.branch,
            capsule.base_sha,
            capsule.lane,
            capsule.run_id,
        ):
            raise ControllerError("durable workspace lease authority changed before external call")
        integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        if (
            integrity.workspace_baseline_head_sha is None
            or integrity.workspace_baseline is None
            or integrity.git_authority_before_sha256 is None
        ):
            raise ControllerError("durable pre-external workspace authority is incomplete")
        predecessors = self.ledger.completed_workspace_predecessors(
            capsule.run_id,
            capsule.milestone_id,
            capsule.workspace_path,
        )
        self._assert_predecessor_terminal_authority(capsule, predecessors)
        baseline, git_authority, protected = self._stable_workspace_authority(
            capsule,
            integrity.workspace_baseline_head_sha,
        )
        self._assert_predecessor_terminal_authority(capsule, predecessors)
        if baseline != integrity.workspace_baseline:
            raise ControllerError(
                "workspace content or topology changed from its durable baseline before external call"
            )
        if git_authority != integrity.git_authority_before_sha256:
            raise ControllerError("Git authority changed from its durable baseline before external call")
        if protected != current.protected_before_sha256:
            raise ControllerError("protected paths changed from their durable baseline before external call")
        return plugin_requirements, snapshots, home

    def _app_controller_state_digest(self) -> str:
        snapshot = {
            path: digest
            for path, digest in self._controller_tree_snapshot(self.state_root).items()
            if path != ".codex-flow/controller.lock" and not path.startswith(".codex-flow/workflow.db")
        }
        return _digest_bytes(_canonical_json(snapshot))

    def prepare_app_native(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> AppNativeDispatchRecord:
        """Prepare one visible App task without constructing or calling an SDK adapter."""

        with self._mutation_lock():
            record = self.ledger.get_execution(run_id, milestone_id)
            existing = self.ledger.get_app_native_for_execution(record.run_id, record.milestone_id)
            if existing is not None:
                return existing
            if record.status is not ExecutionStatus.PLANNED:
                raise ControllerError("App-native prepare cannot replace an SDK-headless execution")
            capsule = self._load_durable_capsule(record)
            try:
                if capsule.plugin_requirements:
                    raise ControllerError("required plugins are unsupported by the App-native route")
            except PluginCapabilityError as exc:
                raise ControllerError(f"plugin capability requirement is malformed: {exc}") from exc
            if capsule.workspace_mode is WorkspaceMode.MANAGED_WORKTREE:
                raise ControllerError("App-native dispatch requires an already selected existing checkout")
            if capsule.output_schema != model_facing_result_schema():
                raise ControllerError("App-native dispatch requires the ModelFacingResult output contract")
            self._worktrees.select(capsule)
            self._assert_protected_clean(capsule)
            predecessors = self.ledger.completed_workspace_predecessors(
                capsule.run_id, capsule.milestone_id, capsule.workspace_path
            )
            self._assert_predecessor_terminal_authority(capsule, predecessors)
            prior_roots = {
                mutable
                for predecessor in predecessors
                for mutable in self._load_durable_capsule(predecessor).mutable_paths
            }
            trusted_roots = tuple(sorted((*prior_roots, *capsule.mutable_paths)))
            self._assert_workspace_history(capsule, capsule.base_sha, trusted_roots)
            self._assert_mutation_scope(capsule, capsule.base_sha, (), trusted_roots)
            self.ledger.acquire_workspace_lease(capsule)
            baseline_head = self._git_output(capsule.workspace_path, "rev-parse", "HEAD")
            baseline, git_before, protected = self._stable_workspace_authority(capsule, baseline_head)
            if protected != record.protected_before_sha256:
                raise ControllerError("protected paths changed between App-native plan and prepare")
            dispatch_id = DispatchId.from_parts(capsule.run_id, capsule.milestone_id, "executor", 1)
            action = AppNativeTaskAction(
                2,
                dispatch_id,
                secrets.token_hex(32),
                secrets.token_hex(32),
                capsule.model,
                capsule.reasoning_effort,
                capsule.workspace_path,
                format_model_facing_result_prompt(capsule.prompt),
                capsule.output_schema,
                result_contract_sha256=model_facing_result_schema_sha256(),
            )
            return self.ledger.prepare_app_native_dispatch(
                capsule.run_id,
                capsule.milestone_id,
                action,
                workspace_baseline_head_sha=baseline_head,
                workspace_baseline=baseline,
                git_authority_before_sha256=git_before,
                controller_state_sha256=self._app_controller_state_digest(),
            )

    def bind_app_native(
        self,
        dispatch_id: DispatchId | str,
        *,
        claim_token: str,
        receipt: HostReceipt,
    ) -> AppNativeDispatchRecord:
        with self._mutation_lock():
            return self.ledger.bind_app_native_dispatch(
                dispatch_id,
                claim_token=claim_token,
                receipt=receipt,
            )

    def complete_app_native(
        self,
        dispatch_id: DispatchId | str,
        *,
        claim_token: str,
        identity: HostIdentity,
        result: ModelFacingResult,
    ) -> AppNativeDispatchRecord:
        """Validate and durably ingest one exact result from the bound App worker."""

        with self._mutation_lock():
            current = self.ledger.get_app_native_dispatch(dispatch_id)
            if not secrets.compare_digest(current.action.claim_token, claim_token):
                raise ControllerError("App-native result capability does not match its prepared action")
            if current.state is AppNativeState.CANCELLED:
                raise ControllerError("cancelled App-native dispatch rejects late native results")
            if current.identity != identity:
                raise ControllerError("App-native result does not match the bound host identity")
            if current.result is not None:
                if current.result != result:
                    raise ControllerError("App-native dispatch already owns a different terminal result")
                return current
            claim = self.ledger.get_dispatch(dispatch_id)
            execution = self.ledger.get_execution(claim.run_id, claim.milestone_id)
            capsule = self._load_durable_capsule(execution)
            self._worktrees.select(capsule)
            validation = _run_validation(capsule.validation, capsule.workspace_path)
            integrity_failed = False
            try:
                protected_after = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
                self._assert_workspace_history(capsule, current.workspace_baseline_head_sha)
                self._assert_mutation_scope(
                    capsule,
                    current.workspace_baseline_head_sha,
                    current.workspace_baseline,
                )
                git_after = git_authority_snapshot(capsule.workspace_path).sha256
                if git_after != current.git_authority_before_sha256:
                    raise ControllerError("Git authority changed during App-native execution")
                if protected_after != execution.protected_before_sha256:
                    raise ControllerError("protected paths changed during App-native execution")
                if self._app_controller_state_digest() != current.controller_state_sha256:
                    raise ControllerError("controller state changed during App-native execution")
            except Exception:
                integrity_failed = True
                protected_after = execution.protected_before_sha256
                git_after = current.git_authority_before_sha256
            if integrity_failed:
                validation = ValidationObservation(
                    validation.argv,
                    validation.exit_code if validation.exit_code != 0 else 125,
                    validation.stdout_sha256,
                    validation.stderr_sha256,
                    validation.timed_out,
                    validation.duration_seconds,
                    ValidationFailureCode.INTEGRITY_FAILURE,
                )
            passed = (
                validation.exit_code == 0
                and not validation.timed_out
                and result.status.value == "completed"
                and all(item.passed for item in result.validations)
            )
            terminal_head: str | None = None
            terminal_workspace: tuple[tuple[str, str], ...] | None = None
            if passed:
                terminal_head = self._git_output(capsule.workspace_path, "rev-parse", "HEAD")
                terminal_workspace = self._owned_workspace_snapshot(capsule)
                if git_authority_snapshot(capsule.workspace_path).sha256 != git_after:
                    raise ControllerError("Git authority changed during App-native terminal capture")
            terminal = self.ledger.record_app_native_result(
                dispatch_id,
                claim_token=claim_token,
                identity=identity,
                result=result,
                validation=validation,
                protected_after_sha256=protected_after,
                git_authority_after_sha256=git_after,
                workspace_terminal_head_sha=terminal_head,
                workspace_terminal=terminal_workspace,
            )
            self._project_execution(self.ledger.get_execution(claim.run_id, claim.milestone_id))
            return terminal

    def app_native_status(self, dispatch_id: DispatchId | str) -> AppNativeDispatchRecord:
        return self.ledger.get_app_native_dispatch(dispatch_id)

    def start(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        with self._mutation_lock():
            return self._start(run_id, milestone_id)

    def _start(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        record = self.ledger.get_execution(run_id, milestone_id)
        capsule = self._load_durable_capsule(record)
        if record.status is not ExecutionStatus.PLANNED:
            if record.status is ExecutionStatus.THREAD_STARTED:
                raise ResumeRequired("execution already has a durable SDK identity; use resume")
            if record.status in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED}:
                return self._finish_terminal(record)
            return record
        if record.checkpoint is ControllerCheckpoint.THREAD_STARTING:
            self.ledger.record_pre_identity_uncertainty(record.run_id, record.milestone_id)
            raise UncertainPreIdentity("prior SDK start crossed the durable external-call boundary")
        self._worktrees.select(capsule)
        self._assert_protected_clean(capsule)
        local_image_inputs = self._validate_local_image_inputs(capsule)
        if record.checkpoint is ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE:
            integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
            if integrity.workspace_baseline_head_sha is None or integrity.workspace_baseline is None:
                raise ControllerError("durable per-milestone workspace baseline is incomplete")
            baseline_head = integrity.workspace_baseline_head_sha
            baseline = integrity.workspace_baseline
            self._authorize_external_call(capsule, record)
            baseline_git_authority = integrity.git_authority_before_sha256
            if baseline_git_authority is None:
                raise ControllerError("durable pre-external Git authority is incomplete")
        else:
            predecessors = self.ledger.completed_workspace_predecessors(
                capsule.run_id, capsule.milestone_id, capsule.workspace_path
            )
            self._assert_predecessor_terminal_authority(capsule, predecessors)
            prior_roots = {
                mutable
                for predecessor in predecessors
                for mutable in self._load_durable_capsule(predecessor).mutable_paths
            }
            trusted_roots = tuple(sorted((*prior_roots, *capsule.mutable_paths)))
            self._assert_workspace_history(capsule, capsule.base_sha, trusted_roots)
            self._assert_mutation_scope(capsule, capsule.base_sha, (), trusted_roots)
            self.ledger.acquire_workspace_lease(capsule)
            baseline_head = self._git_output(capsule.workspace_path, "rev-parse", "HEAD")
            baseline, baseline_git_authority, _ = self._stable_workspace_authority(capsule, baseline_head)
        current_digest = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
        if current_digest != record.protected_before_sha256:
            raise ControllerError("protected paths changed between plan and start")
        self._assert_workspace_history(capsule, baseline_head)
        self._assert_mutation_scope(capsule, baseline_head, baseline)
        self.ledger.acquire_workspace_lease(capsule)
        native_runtime, native_profile_sha256, compatibility_sha256, effective_permission = self._native_runtime(
            capsule
        )
        self.ledger.record_native_profile(
            capsule.run_id,
            capsule.milestone_id,
            native_profile_sha256,
            compatibility_sha256,
            effective_permission,
            baseline_head,
            baseline,
            baseline_git_authority,
        )
        self._fault("after_workspace_baseline")
        if native_runtime is not None:
            native_runtime.native_profile.verify_sources()
        self._authorize_external_call(capsule, record)
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=None,
                cwd=capsule.workspace_path,
                native_runtime=native_runtime,
                permission_mode=(capsule.permission_mode if native_runtime is not None else None),
                effective_permission=(effective_permission if native_runtime is not None else None),
            )
        )
        try:
            self.ledger.claim_dispatch(capsule.run_id, capsule.milestone_id, "executor", 1)
            self.ledger.record_thread_starting(capsule.run_id, capsule.milestone_id)
            try:
                self._fault("before_sdk_thread_start")
            except Exception as exc:
                self.ledger.record_pre_identity_uncertainty(capsule.run_id, capsule.milestone_id)
                raise UncertainPreIdentity(
                    "SDK start failed before durable identity; automatic retry is forbidden"
                ) from exc
            try:
                if native_runtime is not None:
                    native_runtime.native_profile.verify_sources()
                plugin_facts = self._authorize_external_call(capsule, record)
            except Exception:
                self.ledger._reject_thread_start_authorization(capsule.run_id, capsule.milestone_id)
                raise
            skill_binding = _verified_skill_input(*plugin_facts)
            try:
                skill_input = skill_binding.__enter__()
            except Exception:
                self.ledger._reject_thread_start_authorization(capsule.run_id, capsule.milestone_id)
                raise
            try:
                try:
                    identity = adapter.start_thread()
                except Exception as exc:
                    self.ledger.record_pre_identity_uncertainty(capsule.run_id, capsule.milestone_id)
                    raise UncertainPreIdentity(
                        "SDK start failed before durable identity; automatic retry is forbidden"
                    ) from exc
                record = self.ledger.record_thread_identity(capsule.run_id, capsule.milestone_id, identity)
                self._fault("after_thread_identity")
                return self._execute_turn_and_finish(
                    capsule,
                    record,
                    adapter,
                    skill_input=skill_input,
                    local_image_inputs=local_image_inputs,
                )
            finally:
                skill_binding.__exit__(None, None, None)
        finally:
            adapter.close()

    def resume(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        with self._mutation_lock():
            return self._resume(run_id, milestone_id)

    def _resume(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        record = self.ledger.get_execution(run_id, milestone_id)
        if record.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
        }:
            return self._finish_terminal(record)
        if record.status is ExecutionStatus.CANCELLED:
            return record
        if record.status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY:
            raise UncertainPreIdentity("execution has uncertain pre-identity transport state")
        if record.thread_id is None:
            raise ControllerError("execution has no durable SDK identity; use start")
        capsule = self._load_durable_capsule(record)
        self._worktrees.select(capsule)
        self._assert_protected_clean(capsule)
        local_image_inputs = self._validate_local_image_inputs(capsule)
        integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        if integrity.workspace_baseline_head_sha is None or integrity.workspace_baseline is None:
            raise ControllerError("execution lacks a durable per-milestone workspace baseline")
        try:
            native_runtime, native_profile_sha256, compatibility_sha256, candidate_permission = self._native_runtime(
                capsule
            )
        except NativeDiscoveryCompatibilityError as exc:
            raise UnsafeResumeCompatibilityChange(str(exc)) from exc
        try:
            integrity = self.ledger.rebind_native_profile_for_resume(
                capsule.run_id,
                capsule.milestone_id,
                native_profile_sha256,
                compatibility_sha256,
                candidate_permission,
            )
        except NativeCompatibilityConflict as exc:
            raise UnsafeResumeCompatibilityChange(str(exc)) from exc
        except NativePermissionConflict as exc:
            raise UnsafeResumePermissionChange(str(exc)) from exc
        if integrity.effective_permission is None:  # pragma: no cover - controller_v3 schema invariant
            raise UnsafeResumePermissionChange("durable effective native permission is missing")
        if native_runtime is not None:
            try:
                native_runtime.native_profile.verify_sources()
            except NativeDiscoveryCompatibilityError as exc:
                raise UnsafeResumeCompatibilityChange(str(exc)) from exc
        if record.turn_id is not None:
            return self._execute_turn_and_finish(capsule, record, None)
        if record.turn_output == {"__controller_checkpoint": "turn_starting"}:
            raise UncertainTurn("SDK turn crossed the external-call boundary without a durable response")
        if native_runtime is not None:
            try:
                native_runtime.native_profile.verify_sources()
            except NativeDiscoveryCompatibilityError as exc:
                raise UnsafeResumeCompatibilityChange(str(exc)) from exc
        self._authorize_external_call(capsule, record)
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=None,
                cwd=capsule.workspace_path,
                native_runtime=native_runtime,
                permission_mode=(capsule.permission_mode if native_runtime is not None else None),
                effective_permission=(integrity.effective_permission if native_runtime is not None else None),
            )
        )
        try:
            if native_runtime is not None:
                try:
                    native_runtime.native_profile.verify_sources()
                except NativeDiscoveryCompatibilityError as exc:
                    raise UnsafeResumeCompatibilityChange(str(exc)) from exc
            plugin_facts = self._authorize_external_call(capsule, record)
            with _verified_skill_input(*plugin_facts) as skill_input:
                resumed = adapter.resume_thread(record.thread_id)
                if resumed != record.thread_id:
                    raise ControllerError("SDK resume changed durable thread identity")
                return self._execute_turn_and_finish(
                    capsule,
                    record,
                    adapter,
                    skill_input=skill_input,
                    local_image_inputs=local_image_inputs,
                )
        finally:
            adapter.close()

    def _execute_turn_and_finish(
        self,
        capsule: ExecutionCapsule,
        record: ExecutionRecord,
        adapter: Adapter | None,
        *,
        skill_input: SkillInput | None = None,
        local_image_inputs: tuple[LocalImageInput, ...] = (),
    ) -> ExecutionRecord:
        if record.thread_id is None:
            raise ControllerError("cannot execute without a durable SDK identity")
        if record.turn_id is None and record.turn_output == {"__controller_checkpoint": "turn_starting"}:
            raise UncertainTurn("SDK turn crossed the external-call boundary without a durable response")
        controller_state_error = ""
        integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        if record.turn_id is None:
            if adapter is None:
                raise ControllerError("cannot execute an SDK turn without an adapter")
            self._authorize_external_call(capsule, record)
            git_before = git_authority_snapshot(capsule.workspace_path)
            integrity = self.ledger.record_git_authority_before(
                capsule.run_id,
                capsule.milestone_id,
                git_before.sha256,
            )
            self.ledger.record_turn_starting(capsule.run_id, capsule.milestone_id)
            controller_state_before = self._controller_tree_snapshot(self.state_root)
            self._fault("before_sdk_turn")
            try:
                self._authorize_external_call(capsule, record)
            except Exception:
                self.ledger._reject_turn_authorization(capsule.run_id, capsule.milestone_id)
                raise
            sdk_input: str | SkillInput = capsule.prompt
            if capsule.plugin_requirements:
                if skill_input is None:
                    raise ControllerError("verified bundled skill input is unavailable")
                sdk_input = skill_input
            if local_image_inputs:
                observation = adapter.run_turn(
                    record.thread_id,
                    sdk_input,
                    local_image_inputs=local_image_inputs,
                    output_schema=capsule.output_schema,
                )
            else:
                observation = adapter.run_turn(record.thread_id, sdk_input, output_schema=capsule.output_schema)
            try:
                if controller_state_before != self._controller_tree_snapshot(self.state_root):
                    controller_state_error = "controller_state_mutated"
            except ControllerError:
                controller_state_error = "controller_state_integrity_failure"
            record = self.ledger.record_turn(capsule.run_id, capsule.milestone_id, observation)
            self._fault("after_turn")
        turn_output = record.turn_output or {}
        validation = _run_validation(capsule.validation, capsule.workspace_path)
        protected_digest_error = False
        try:
            protected_after = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
        except Exception:
            # The result still has to be committed atomically even when a
            # protected-path read itself is no longer trustworthy.  Retain the
            # last known digest as a typed integrity-failure terminal fact;
            # never persist the platform exception text.
            protected_after = record.protected_before_sha256
            protected_digest_error = True
        git_authority_error = False
        try:
            git_after = git_authority_snapshot(capsule.workspace_path)
        except Exception:
            git_after_sha256 = integrity.git_authority_before_sha256 or ("0" * 64)
            git_authority_error = True
        else:
            git_after_sha256 = git_after.sha256
        git_authority_changed = (
            integrity.git_authority_before_sha256 is None or git_after_sha256 != integrity.git_authority_before_sha256
        )
        result: JsonObject = turn_output
        if validation.error_code is ValidationFailureCode.EXECUTABLE_UNAVAILABLE:
            result = {"status": "failed", "reason": "validation_executable_unavailable"}
        elif validation.error_code is ValidationFailureCode.NONZERO_EXIT:
            result = {"status": "failed", "reason": "validation_failed"}
        elif validation.error_code is ValidationFailureCode.TIMEOUT:
            result = {"status": "failed", "reason": "validation_timeout"}
        else:
            structured_status = turn_output.get("status")
            if isinstance(structured_status, str) and structured_status.strip().lower() in {
                "failed",
                "failure",
                "blocked",
                "cancelled",
                "error",
            }:
                result = {"status": "failed", "reason": "sdk_terminal_outcome_failed"}
        mutation_scope_valid = True
        try:
            if integrity.workspace_baseline_head_sha is None or integrity.workspace_baseline is None:
                raise ControllerError("execution lacks a durable per-milestone workspace baseline")
            self._assert_workspace_history(capsule, integrity.workspace_baseline_head_sha)
            self._assert_mutation_scope(
                capsule,
                integrity.workspace_baseline_head_sha,
                integrity.workspace_baseline,
            )
        except ControllerError as exc:
            mutation_scope_valid = False
            scope_error = str(exc)
        except Exception:
            mutation_scope_valid = False
            scope_error = "workspace_integrity_failure"
        else:
            scope_error = ""
        if protected_digest_error:
            validation = ValidationObservation(
                validation.argv,
                validation.exit_code if validation.exit_code != 0 else 125,
                validation.stdout_sha256,
                validation.stderr_sha256,
                validation.timed_out,
                validation.duration_seconds,
                ValidationFailureCode.INTEGRITY_FAILURE,
            )
            result = {"status": "failed", "reason": "protected_paths_integrity_failure"}
        elif (
            protected_after != record.protected_before_sha256
            or not mutation_scope_valid
            or controller_state_error
            or git_authority_error
            or git_authority_changed
        ):
            failure_code = (
                ValidationFailureCode.INTEGRITY_FAILURE
                if (
                    controller_state_error
                    or protected_after != record.protected_before_sha256
                    or git_authority_error
                    or git_authority_changed
                )
                else validation.error_code
            )
            validation = ValidationObservation(
                validation.argv,
                validation.exit_code if validation.exit_code != 0 else 125,
                validation.stdout_sha256,
                validation.stderr_sha256,
                validation.timed_out,
                validation.duration_seconds,
                failure_code,
            )
            if protected_after != record.protected_before_sha256:
                integrity_reason = "protected_paths_changed"
            elif controller_state_error:
                integrity_reason = "controller_state_mutated"
            elif not mutation_scope_valid:
                integrity_reason = (
                    "workspace_integrity_failure"
                    if scope_error == "workspace_integrity_failure"
                    else "branch_or_history_changed"
                    if scope_error.startswith(
                        (
                            "workspace branch",
                            "workspace history",
                            "unable to inspect Git workspace: symbolic-ref",
                        )
                    )
                    else "mutation_outside_owned_paths"
                )
            elif git_authority_error:
                integrity_reason = "git_authority_integrity_failure"
            else:
                integrity_reason = "git_authority_changed"
            result = {"status": "failed", "reason": integrity_reason}
        outcome_status = result.get("status") if isinstance(result, Mapping) else None
        outcome_failed = isinstance(outcome_status, str) and outcome_status.strip().lower() in {
            "failed",
            "failure",
            "blocked",
            "cancelled",
            "error",
        }
        terminal_head: str | None = None
        terminal_workspace: tuple[tuple[str, str], ...] | None = None
        if validation.exit_code == 0 and not validation.timed_out and not outcome_failed:
            try:
                terminal_head = self._git_output(capsule.workspace_path, "rev-parse", "HEAD")
                terminal_workspace = self._owned_workspace_snapshot(capsule)
                if git_authority_snapshot(capsule.workspace_path).sha256 != git_after_sha256:
                    raise ControllerError("Git authority changed during terminal workspace capture")
            except Exception:
                validation = ValidationObservation(
                    validation.argv,
                    125,
                    validation.stdout_sha256,
                    validation.stderr_sha256,
                    validation.timed_out,
                    validation.duration_seconds,
                    ValidationFailureCode.INTEGRITY_FAILURE,
                )
                result = {"status": "failed", "reason": "workspace_integrity_failure"}
                terminal_head = None
                terminal_workspace = None
        terminal = self.ledger.record_terminal_execution(
            capsule.run_id,
            capsule.milestone_id,
            result=result,
            validation=validation,
            protected_after_sha256=protected_after,
            git_authority_after_sha256=git_after_sha256,
            workspace_terminal_head_sha=terminal_head,
            workspace_terminal=terminal_workspace,
        )
        self._fault("before_projection")
        self._project_execution(terminal)
        return terminal

    @staticmethod
    def _validate_local_image_inputs(capsule: ExecutionCapsule) -> tuple[LocalImageInput, ...]:
        if not capsule.local_image_paths:
            return ()
        try:
            return validate_local_image_inputs(capsule.local_image_paths, workspace=capsule.workspace_path)
        except (OSError, ValueError) as exc:
            raise ControllerError("local image input is invalid before SDK start") from exc

    def _finish_terminal(self, record: ExecutionRecord) -> ExecutionRecord:
        state = self.ledger.current_state(record.run_id, record.milestone_id)
        target = WorkflowState.COMPLETED if record.status is ExecutionStatus.COMPLETED else WorkflowState.FAILED
        if state is WorkflowState.RUNNING:
            self.ledger.transition(
                record.run_id,
                record.milestone_id,
                target,
                expected_state=state,
                reason=(
                    ReasonCode.TERMINAL_OUTCOME if target is WorkflowState.COMPLETED else ReasonCode.EXECUTION_FAILURE
                ),
            )
        elif state is not target:
            raise ControllerError(
                f"terminal execution status {record.status.value} conflicts with milestone state {state.value}"
            )
        self._project_execution(record)
        return record

    def _project_execution(self, record: ExecutionRecord) -> None:
        target = self.state_dir / "runs" / str(record.run_id) / "execution.json"
        try:
            integrity = self.ledger.get_execution_integrity(record.run_id, record.milestone_id)
        except KeyError:
            integrity = None
        payload = {
            "run_id": str(record.run_id),
            "milestone_id": str(record.milestone_id),
            "status": record.status.value,
            "checkpoint": record.checkpoint.value,
            "workspace_path": str(record.workspace_path),
            "thread_id": record.thread_id.id if record.thread_id else None,
            "turn_id": record.turn_id,
            "result": record.result,
            "integrity_provenance": integrity.provenance if integrity else None,
            "native_profile_sha256": integrity.native_profile_sha256 if integrity else None,
            "native_compatibility_sha256": integrity.native_compatibility_sha256 if integrity else None,
            "effective_permission": (
                integrity.effective_permission.facts if integrity and integrity.effective_permission else None
            ),
            "effective_permission_sha256": integrity.effective_permission_sha256 if integrity else None,
            "git_authority_before_sha256": integrity.git_authority_before_sha256 if integrity else None,
            "git_authority_after_sha256": integrity.git_authority_after_sha256 if integrity else None,
            "workspace_terminal_sha256": integrity.workspace_terminal_sha256 if integrity else None,
            "validation": (
                {
                    "argv": list(record.validation.argv),
                    "exit_code": record.validation.exit_code,
                    "stdout_sha256": record.validation.stdout_sha256,
                    "stderr_sha256": record.validation.stderr_sha256,
                    "timed_out": record.validation.timed_out,
                    "duration_seconds": record.validation.duration_seconds,
                    "error_code": record.validation.error_code.value if record.validation.error_code else None,
                }
                if record.validation
                else None
            ),
        }
        content = _canonical_json(payload)
        write_owned_artifact(
            self.state_root,
            target.relative_to(self.state_root),
            content,
            replace=True,
        )

    def status(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        return self.ledger.get_execution(run_id, milestone_id)

    def cancel(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        with self._mutation_lock():
            return self._cancel(run_id, milestone_id)

    def _cancel(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        current = self.ledger.get_execution(run_id, milestone_id)
        if current.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return current
        return self.ledger.cancel_execution(run_id, milestone_id)


def execution_json(record: ExecutionRecord) -> JsonObject:
    return {
        "run_id": str(record.run_id),
        "milestone_id": str(record.milestone_id),
        "status": record.status.value,
        "checkpoint": record.checkpoint.value,
        "workspace_path": str(record.workspace_path),
        "capsule_path": str(record.capsule_path),
        "capsule_sha256": record.capsule_sha256,
        "model": record.model,
        "reasoning_effort": record.reasoning_effort.value,
        "thread_id": record.thread_id.id if record.thread_id else None,
        "turn_id": record.turn_id,
        "result": record.result,
        "validation": (
            {
                "argv": list(record.validation.argv),
                "exit_code": record.validation.exit_code,
                "stdout_sha256": record.validation.stdout_sha256,
                "stderr_sha256": record.validation.stderr_sha256,
                "timed_out": record.validation.timed_out,
                "duration_seconds": record.validation.duration_seconds,
                "error_code": record.validation.error_code.value if record.validation.error_code else None,
            }
            if record.validation
            else None
        ),
        "protected_before_sha256": record.protected_before_sha256,
        "protected_after_sha256": record.protected_after_sha256,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def queue_json(row: Mapping[str, object]) -> JsonObject:
    """Public queue projection omitting prompts, tokens and raw result text."""

    allowed = {
        "dispatch_id",
        "run_id",
        "milestone_id",
        "role",
        "generation",
        "backend",
        "workspace_path",
        "result_contract_sha256",
        "state",
        "sequence",
        "available_at",
        "deadline",
        "claim_epoch",
        "attempt",
        "thread_id",
        "host_id",
        "raw_result_sha256",
        "terminal_status",
        "created_at",
        "updated_at",
    }
    return {key: row[key] for key in allowed if key in row}


class ReviewWorkflow:
    """Objective execution/review/repair loop over the existing SDK ledger.

    The callbacks are deliberately narrow seams: the executor may call the
    existing :class:`Controller`/SDK adapter, while reviewers receive only a
    revision token and can never mutate the workspace through this class.
    """

    def __init__(self, ledger: Ledger, repository_root: str | Path, config: WorkflowConfig | None = None) -> None:
        self.ledger = ledger
        self.repository_root = Path(repository_root).resolve()
        self.config = config

    @staticmethod
    def classify_recovery(
        *,
        intent_unchanged: bool = True,
        contract_unchanged: bool = True,
        security_boundary_unchanged: bool = True,
        cost_unchanged: bool = True,
        destructive_behavior_unchanged: bool = True,
        scope_unchanged: bool = True,
        underdetermined: bool = False,
        external_prerequisite: str | None = None,
        proven_infeasible: bool = False,
        checkpoint: str = "durable_checkpoint",
    ) -> RecoveryDecision:
        """Classify recovery deterministically without attempt counters."""

        if external_prerequisite is not None:
            return RecoveryDecision(
                RecoveryOutcome.EXTERNAL_BLOCKED,
                "an external prerequisite is unavailable",
                checkpoint,
                external_prerequisite=external_prerequisite,
            )
        if underdetermined:
            return RecoveryDecision(
                RecoveryOutcome.NEEDS_DECISION,
                "accepted intent or authority is underdetermined",
                checkpoint,
                decision_request_id="decision-required",
            )
        if proven_infeasible:
            return RecoveryDecision(
                RecoveryOutcome.FAILED,
                "evidence shows the accepted goal is infeasible under its constraints",
                checkpoint,
            )
        unchanged = (
            intent_unchanged,
            contract_unchanged,
            security_boundary_unchanged,
            cost_unchanged,
            destructive_behavior_unchanged,
            scope_unchanged,
        )
        if all(unchanged):
            return RecoveryDecision(
                RecoveryOutcome.CONTINUE_WITH_REPLAN,
                "the accepted contract and safety boundary remain unchanged",
                checkpoint,
            )
        return RecoveryDecision(
            RecoveryOutcome.CHANGE_STRATEGY,
            "implementation strategy must change while the accepted outcome remains fixed",
            checkpoint,
        )

    @staticmethod
    def _finding_json(finding: ReviewFinding) -> JsonObject:
        return {
            "finding_id": finding.finding_id,
            "causal_class": finding.causal_class.value,
            "severity": finding.severity.value,
            "promotion_blocking": finding.promotion_blocking,
            "promotion_reason": finding.promotion_reason,
            "evidence": thaw_json(finding.evidence),
            "criterion": finding.criterion,
            "defer_to": finding.defer_to,
            "survives_prior_repair": finding.survives_prior_repair,
        }

    @classmethod
    def _review_json(cls, result: ReviewResult) -> JsonObject:
        return {
            "review_id": result.review_id,
            "reviewer_role": str(result.reviewer_role),
            "accepted": result.accepted,
            "reviewed_revision": result.reviewed_revision,
            "fresh": result.fresh,
            "read_only": result.read_only,
            "prior_review_id": result.prior_review_id,
            "acceptance_mode": result.acceptance_mode.value,
            "evidence_ids": list(result.evidence_ids),
            "findings": [cls._finding_json(finding) for finding in result.findings],
        }

    def _project_result(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        status: str,
        accepted: bool,
        reviews: tuple[ReviewResult, ...],
        repairs: tuple[RepairRecord, ...],
        recovery: RecoveryDecision | None = None,
        authority_plan: AuthorityPlan | None = None,
        rendered_evidence: tuple[RenderedEvidence, ...] = (),
    ) -> ReviewLifecycleResult:
        if recovery is not None:
            existing_recovery = any(
                record.kind == "recovery_decision"
                and record.data.get("outcome") == recovery.outcome.value
                and record.data.get("checkpoint") == recovery.checkpoint
                for record in self.ledger.review_lifecycle(run_id, milestone_id)
            )
            if not existing_recovery:
                self.ledger.record_review_recovery(run_id, milestone_id, recovery)
        lifecycle = tuple(
            LifecycleRecord(
                record.phase,
                record.kind,
                record.sequence,
                thaw_json(record.data),
            )
            for record in self.ledger.review_lifecycle(run_id, milestone_id)
        )
        result = ReviewLifecycleResult(
            status,
            accepted,
            tuple(review.review_id for review in reviews),
            tuple(finding.finding_id for review in reviews for finding in review.findings),
            tuple(repair.repair_id for repair in repairs),
            lifecycle,
            recovery,
            authority_plan,
            rendered_evidence,
        )
        write_review_artifact(self.repository_root, run_id, str(milestone_id), result)
        return result

    def run(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        objective: Callable[[], JsonObject],
        reviewer: Callable[[str, bool], ReviewResult],
        repair: Callable[[ReviewFinding], RepairRecord],
        current_revision: Callable[[], str],
        budget: Budget | None = None,
    ) -> ReviewLifecycleResult:
        """Run one bounded objective -> review -> repair -> fresh review loop."""

        active_budget = budget or Budget(max_turns=2, max_repairs=1, max_reviews=2, max_validations=2)
        current = self.ledger.current_state(run_id, milestone_id)
        if current is WorkflowState.PLANNED:
            self.ledger.claim_dispatch(run_id, milestone_id, "executor", 1)
            self.ledger.transition(run_id, milestone_id, WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
            current = WorkflowState.RUNNING
        if current is not WorkflowState.RUNNING:
            raise ControllerError(f"review objective requires RUNNING workflow state, found {current.value}")
        exhausted = active_budget.exhausted()
        if exhausted is not None:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint=f"budget:{exhausted.value}")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.RUNNING,
                phase=LifecyclePhase.BUDGET,
                kind="limit_exhausted",
                data={"limit": exhausted.value},
                reason=ReasonCode.EXECUTION_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="limit_exhausted",
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
            )
        active_budget = replace(active_budget, turns=active_budget.turns + 1)
        try:
            objective_output = objective()
            if not isinstance(objective_output, Mapping):
                raise ValueError("objective output must be a JSON object")
        except (TransportFailureBeforeIdentity, TerminalFailureAfterIdentity) as exc:
            recovery = self.classify_recovery(
                checkpoint="objective_transport_boundary",
            )
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.RUNNING,
                phase=LifecyclePhase.RECOVERY,
                kind="transport_failure",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.TRANSPORT_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="transport_failure",
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
            )
        except Exception as exc:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="objective_failure")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.RUNNING,
                phase=LifecyclePhase.RECOVERY,
                kind="objective_failed",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.EXECUTION_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="failed",
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
            )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.COMPLETED,
            expected_state=WorkflowState.RUNNING,
            phase=LifecyclePhase.EXECUTION,
            kind="objective_completed",
            data={"output": dict(objective_output)},
        )

        reviews: list[ReviewResult] = []
        repairs: list[RepairRecord] = []
        revision = current_revision()
        if active_budget.reviews >= active_budget.max_reviews:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="review_limit")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.COMPLETED,
                phase=LifecyclePhase.BUDGET,
                kind="limit_exhausted",
                data={"limit": "reviews"},
                reason=ReasonCode.EXECUTION_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="limit_exhausted",
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
            )
        active_budget = replace(active_budget, reviews=active_budget.reviews + 1)
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="review_started",
            data={"review_index": 1, "revision": revision, "read_only": True},
        )
        first = reviewer(revision, True)
        if first.reviewed_revision != revision or not first.fresh or not first.read_only:
            recovery = self.classify_recovery(checkpoint="stale_review")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.REPAIR_REQUIRED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="stale_review",
                data={
                    "review_id": first.review_id,
                    "expected_revision": revision,
                    "reviewed_revision": first.reviewed_revision,
                },
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="stale_review",
                accepted=False,
                reviews=(first,),
                repairs=(),
                recovery=recovery,
            )
        reviews.append(first)
        blockers = first.promotion_blockers
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REPAIR_REQUIRED if blockers else WorkflowState.ACCEPTED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.REVIEW,
            kind="review_rejected" if blockers else "review_accepted",
            data={"review": self._review_json(first)},
            reason=ReasonCode.REVIEW_REJECTED if blockers else ReasonCode.TERMINAL_OUTCOME,
        )
        if not blockers:
            return self._project_result(
                run_id, milestone_id, status="accepted", accepted=True, reviews=tuple(reviews), repairs=()
            )

        if len(blockers) > active_budget.max_repairs - active_budget.repairs:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="repair_limit")
            return self._project_result(
                run_id,
                milestone_id,
                status="limit_exhausted",
                accepted=False,
                reviews=tuple(reviews),
                repairs=(),
                recovery=recovery,
            )
        finding = blockers[0]
        repair_record = repair(finding)
        if repair_record.finding_id != finding.finding_id or not repair_record.same_owner:
            raise ControllerError("repair must acknowledge the exact finding and same durable owner")
        repairs.append(repair_record)
        active_budget = replace(active_budget, repairs=active_budget.repairs + 1)
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.RUNNING,
            expected_state=WorkflowState.REPAIR_REQUIRED,
            phase=LifecyclePhase.REPAIR,
            kind="repair_completed",
            data={
                "repair_id": repair_record.repair_id,
                "finding_id": repair_record.finding_id,
                "same_owner": repair_record.same_owner,
                "outcome": repair_record.outcome,
            },
            reason=ReasonCode.REVIEW_REJECTED,
        )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.COMPLETED,
            expected_state=WorkflowState.RUNNING,
            phase=LifecyclePhase.EXECUTION,
            kind="repair_execution_completed",
            data={"repair_id": repair_record.repair_id},
        )
        fresh_revision = current_revision()
        if active_budget.reviews >= active_budget.max_reviews:
            raise ControllerError("review limit exhausted before fresh re-review")
        active_budget = replace(active_budget, reviews=active_budget.reviews + 1)
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="review_started",
            data={"review_index": 2, "revision": fresh_revision, "read_only": True, "fresh": True},
        )
        second = reviewer(fresh_revision, True)
        if second.reviewed_revision != fresh_revision or not second.fresh or not second.read_only:
            recovery = self.classify_recovery(checkpoint="stale_re_review")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.REPAIR_REQUIRED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="stale_review",
                data={
                    "review_id": second.review_id,
                    "expected_revision": fresh_revision,
                    "reviewed_revision": second.reviewed_revision,
                },
                reason=ReasonCode.REVIEW_REJECTED,
            )
            reviews.append(second)
            return self._project_result(
                run_id,
                milestone_id,
                status="stale_review",
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=recovery,
            )
        prior_ids = {finding.finding_id for finding in first.findings}
        for finding in second.findings:
            if finding.finding_id in prior_ids and not finding.survives_prior_repair:
                raise ControllerError("a surviving finding must explicitly survive the exact prior repair")
            if finding.finding_id not in prior_ids and finding.survives_prior_repair:
                raise ControllerError("new review scope cannot masquerade as a surviving finding")
        reviews.append(second)
        if second.promotion_blockers:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="re_review_rejected")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="re_review_rejected",
                data={"review": self._review_json(second)},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status="rejected_after_repair",
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=recovery,
            )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.ACCEPTED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.ACCEPTANCE,
            kind="review_accepted",
            data={"review": self._review_json(second), "same_owner_repair": True},
            reason=ReasonCode.TERMINAL_OUTCOME,
        )
        return self._project_result(
            run_id, milestone_id, status="accepted", accepted=True, reviews=tuple(reviews), repairs=tuple(repairs)
        )

    @staticmethod
    def _authority_json(plan: AuthorityPlan) -> JsonObject:
        return {
            "assignments": [
                {
                    "mode": assignment.mode.value if assignment.mode is not None else None,
                    "role": str(assignment.role),
                    "model": assignment.model,
                    "reasoning_effort": assignment.reasoning_effort.value,
                }
                for assignment in plan.assignments
            ]
        }

    @staticmethod
    def _evidence_json(evidence: RenderedEvidence) -> JsonObject:
        return {
            "evidence_id": evidence.evidence_id,
            "revision": evidence.revision,
            "artifact_sha256": evidence.artifact_sha256,
            "artifact_path": evidence.artifact_path,
            "width": evidence.width,
            "height": evidence.height,
        }

    def run_multi_authority(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        objective: Callable[[], JsonObject],
        repair: Callable[[ReviewFinding], RepairRecord],
        current_revision: Callable[[], str],
        acceptance_modes: Sequence[AcceptanceMode | str] | None = None,
        capsule: ExecutionCapsule | None = None,
        reviewers: Mapping[AcceptanceMode | str, Callable[..., ReviewResult]] | None = None,
        objective_reviewer: Callable[[str, bool], ReviewResult] | None = None,
        visual_reviewer: Callable[[str, RenderedEvidence, bool], ReviewResult] | None = None,
        architecture_reviewer: Callable[[str, RenderedEvidence | None, bool], ReviewResult] | None = None,
        render_evidence: Callable[[str], RenderedEvidence] | None = None,
        architecture_replan: Callable[[ReviewFinding], ReplanProposal] | None = None,
        planner: Callable[[DecisionRequest], DecisionResponse] | None = None,
        notification: Callable[[ReviewLifecycleResult], None] | None = None,
        budget: Budget | None = None,
        available_models: set[str] | None = None,
    ) -> ReviewLifecycleResult:
        """Run the complete bounded objective/visual/architecture review route.

        All mode reviewers are read-only and independent.  The first review
        pass is complete before any repair is attempted; a rejection is then
        diagnosed by the recovery authority and, for an unchanged boundary,
        recorded as a bounded ``CONTINUE_WITH_REPLAN`` before the same owner
        repairs in the leased workspace.
        """

        declared_modes = (
            capsule.acceptance_modes if capsule is not None else (acceptance_modes or (AcceptanceMode.OBJECTIVE,))
        )
        modes = tuple(mode if isinstance(mode, AcceptanceMode) else AcceptanceMode(mode) for mode in declared_modes)
        config = self.config or load_workflow_config(self.repository_root / "workflow.toml")
        plan = config.derive_authorities(modes, available_models=available_models)
        callback_map: dict[AcceptanceMode, Callable[..., ReviewResult]] = {}
        if reviewers is not None:
            for raw_mode, callback in reviewers.items():
                mode = raw_mode if isinstance(raw_mode, AcceptanceMode) else AcceptanceMode(raw_mode)
                callback_map[mode] = callback
        if objective_reviewer is not None:
            callback_map[AcceptanceMode.OBJECTIVE] = objective_reviewer
        if visual_reviewer is not None:
            callback_map[AcceptanceMode.VISUAL] = visual_reviewer
        if architecture_reviewer is not None:
            callback_map[AcceptanceMode.ARCHITECTURE] = architecture_reviewer
        missing_callbacks = [mode.value for mode in modes if mode not in callback_map]
        if missing_callbacks:
            raise AuthorityUnavailable("required reviewer callbacks are unavailable: " + ",".join(missing_callbacks))
        if AcceptanceMode.VISUAL in modes and render_evidence is None:
            raise AuthorityUnavailable("visual acceptance requires fixed rendered evidence")

        active_budget = budget or config.limits.budget()
        reviews: list[ReviewResult] = []
        repairs: list[RepairRecord] = []
        evidence: list[RenderedEvidence] = []
        recovery: RecoveryDecision | None = None

        if self.ledger.current_state(run_id, milestone_id) is WorkflowState.PLANNED:
            self.ledger.claim_dispatch(run_id, milestone_id, "executor", 1)
            self.ledger.transition(run_id, milestone_id, WorkflowState.RUNNING, expected_state=WorkflowState.STARTING)
        if self.ledger.current_state(run_id, milestone_id) is not WorkflowState.RUNNING:
            raise ControllerError("multi-authority review requires a running milestone")
        self.ledger.record_review_fact(
            run_id,
            milestone_id,
            phase=LifecyclePhase.ROUTING,
            kind="authorities_derived",
            data={"acceptance_modes": [mode.value for mode in modes], "authority_plan": self._authority_json(plan)},
        )

        def budget_failure(kind: str) -> ReviewLifecycleResult:
            nonlocal recovery
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint=f"budget:{kind}")
            self.ledger.record_review_fact(
                run_id,
                milestone_id,
                phase=LifecyclePhase.BUDGET,
                kind="limit_exhausted",
                data={"limit": kind},
            )
            state = self.ledger.current_state(run_id, milestone_id)
            if state not in {WorkflowState.FAILED, WorkflowState.ACCEPTED}:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.FAILED,
                    expected_state=state,
                    phase=LifecyclePhase.BUDGET,
                    kind="limit_exhausted",
                    data={"limit": kind},
                    reason=ReasonCode.EXECUTION_FAILURE,
                )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.LIMIT_EXHAUSTED.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=recovery,
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )

        exhausted = active_budget.exhausted()
        if exhausted is not None:
            return budget_failure(exhausted.value)
        active_budget = replace(active_budget, turns=active_budget.turns + 1)
        try:
            output = objective()
            if not isinstance(output, Mapping):
                raise ValueError("objective output must be a JSON object")
        except (TransportFailureBeforeIdentity, TerminalFailureAfterIdentity) as exc:
            recovery = self.classify_recovery(checkpoint="objective_transport_boundary")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.RUNNING,
                phase=LifecyclePhase.RECOVERY,
                kind="transport_failure",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.TRANSPORT_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.TRANSPORT_FAILURE.value,
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
                authority_plan=plan,
            )
        except Exception as exc:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="objective_failure")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.RUNNING,
                phase=LifecyclePhase.RECOVERY,
                kind="objective_failed",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.EXECUTION_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.FAILED.value,
                accepted=False,
                reviews=(),
                repairs=(),
                recovery=recovery,
                authority_plan=plan,
            )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.COMPLETED,
            expected_state=WorkflowState.RUNNING,
            phase=LifecyclePhase.EXECUTION,
            kind="objective_completed",
            data={"output": dict(output)},
        )
        revision = current_revision()
        if AcceptanceMode.VISUAL in modes:
            try:
                rendered = render_evidence(revision)  # type: ignore[misc]
            except Exception as exc:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.FAILED,
                    expected_state=WorkflowState.COMPLETED,
                    phase=LifecyclePhase.RECOVERY,
                    kind="rendered_evidence_failed",
                    data={"exception_class": type(exc).__name__},
                    reason=ReasonCode.EXECUTION_FAILURE,
                )
                return self._project_result(
                    run_id,
                    milestone_id,
                    status=LifecycleStatus.FAILED.value,
                    accepted=False,
                    reviews=(),
                    repairs=(),
                    recovery=self.classify_recovery(proven_infeasible=True, checkpoint="rendered_evidence"),
                    authority_plan=plan,
                )
            if rendered.revision != revision:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.FAILED,
                    expected_state=WorkflowState.COMPLETED,
                    phase=LifecyclePhase.RECOVERY,
                    kind="stale_rendered_evidence",
                    data={"expected_revision": revision},
                    reason=ReasonCode.REVIEW_REJECTED,
                )
                return self._project_result(
                    run_id,
                    milestone_id,
                    status=LifecycleStatus.STALE_REVIEW.value,
                    accepted=False,
                    reviews=(),
                    repairs=(),
                    recovery=self.classify_recovery(checkpoint="rendered_evidence"),
                    authority_plan=plan,
                )
            evidence.append(rendered)
            self.ledger.record_review_fact(
                run_id,
                milestone_id,
                phase=LifecyclePhase.REVIEW,
                kind="rendered_evidence_fixed",
                data=self._evidence_json(rendered),
            )

        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="review_round_started",
            data={"round": 1, "revision": revision, "acceptance_modes": [mode.value for mode in modes]},
        )

        def invoke(mode: AcceptanceMode, token: str, fresh: bool) -> ReviewResult:
            callback = callback_map[mode]
            rendered = evidence[0] if evidence else None
            if mode is AcceptanceMode.OBJECTIVE:
                result = callback(token, fresh)
            elif mode is AcceptanceMode.VISUAL:
                result = callback(token, rendered, fresh)
            else:
                # Architecture callbacks may choose either the rendered
                # evidence-aware form or the objective-style two-argument
                # form.  Inspecting arity avoids invoking an SDK callback
                # twice after a consumed turn.
                try:
                    parameter_count = len(inspect.signature(callback).parameters)
                except (TypeError, ValueError):
                    parameter_count = 3
                result = callback(token, rendered, fresh) if parameter_count >= 3 else callback(token, fresh)
            if result.reviewer_role != plan.for_mode(mode).role:
                raise ControllerError(f"stale authority for {mode.value} review")
            if result.acceptance_mode is not mode:
                raise ControllerError(f"reviewer returned the wrong acceptance mode for {mode.value}")
            if not result.fresh or not result.read_only or result.reviewed_revision != token:
                raise ControllerError(f"stale {mode.value} reviewer result")
            if mode is AcceptanceMode.VISUAL and evidence[0].evidence_id not in result.evidence_ids:
                raise ControllerError("visual review did not acknowledge fixed rendered evidence")
            self.ledger.record_review_fact(
                run_id,
                milestone_id,
                phase=LifecyclePhase.REVIEW,
                kind="authority_review",
                data={"mode": mode.value, "review": self._review_json(result), "round": 1},
            )
            return result

        try:
            first_reviews = [invoke(mode, revision, True) for mode in modes]
        except AuthorityUnavailable as exc:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="route_unavailable",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.ENVIRONMENT_BLOCKED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.ROUTE_UNAVAILABLE.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        except ControllerError as exc:
            recovery = self.classify_recovery(checkpoint="stale_authority")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="stale_authority",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.STALE_AUTHORITY.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=recovery,
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        except Exception as exc:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="review_failed",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.FAILED.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        reviews.extend(first_reviews)
        blockers = [finding for review in first_reviews for finding in review.promotion_blockers]
        if not blockers:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.ACCEPTED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.ACCEPTANCE,
                kind="all_authorities_accepted",
                data={"review_ids": [review.review_id for review in first_reviews]},
                reason=ReasonCode.TERMINAL_OUTCOME,
            )
            result = self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.ACCEPTED.value,
                accepted=True,
                reviews=tuple(reviews),
                repairs=(),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
            if notification is not None:
                try:
                    notification(result)
                except Exception as exc:
                    self.ledger.record_review_fact(
                        run_id,
                        milestone_id,
                        phase=LifecyclePhase.ACCEPTANCE,
                        kind="notification_failure",
                        data={"exception_class": type(exc).__name__},
                    )
                    result = replace(
                        result,
                        notification_failure=True,
                        lifecycle=tuple(
                            LifecycleRecord(item.phase, item.kind, item.sequence, thaw_json(item.data))
                            for item in self.ledger.review_lifecycle(run_id, milestone_id)
                        ),
                    )
                    write_review_artifact(self.repository_root, run_id, str(milestone_id), result)
            return result

        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REPAIR_REQUIRED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.REVIEW,
            kind="multi_authority_rejected",
            data={"finding_ids": [finding.finding_id for finding in blockers]},
            reason=ReasonCode.REVIEW_REJECTED,
        )
        architecture_blockers = [
            finding
            for review in first_reviews
            if review.acceptance_mode is AcceptanceMode.ARCHITECTURE
            for finding in review.promotion_blockers
        ]
        if architecture_blockers:
            finding = architecture_blockers[0]
            if architecture_replan is None:
                raise AuthorityUnavailable("architecture rejection requires a recovery/replan authority")
            proposal = architecture_replan(finding)
            if not isinstance(proposal, ReplanProposal) or not proposal.boundary_unchanged:
                request = DecisionRequest(
                    "architecture-replan-decision",
                    "The proposed architecture strategy changes an accepted boundary; confirm a new authorized scope.",
                    ("keep-accepted-boundary", "request-user-decision"),
                )
                self.ledger.record_review_decision_request(run_id, milestone_id, request)
                if planner is not None:
                    response = planner(request)
                    if not isinstance(response, DecisionResponse) or response.request_id != request.request_id:
                        raise ControllerError("planner returned an invalid decision response")
                    self.ledger.record_review_decision_response(run_id, milestone_id, response)
                recovery = self.classify_recovery(
                    intent_unchanged=isinstance(proposal, ReplanProposal) and proposal.intent_unchanged,
                    contract_unchanged=isinstance(proposal, ReplanProposal) and proposal.contract_unchanged,
                    security_boundary_unchanged=isinstance(proposal, ReplanProposal)
                    and proposal.security_boundary_unchanged,
                    cost_unchanged=isinstance(proposal, ReplanProposal) and proposal.cost_unchanged,
                    destructive_behavior_unchanged=isinstance(proposal, ReplanProposal)
                    and proposal.destructive_behavior_unchanged,
                    scope_unchanged=isinstance(proposal, ReplanProposal) and proposal.scope_unchanged,
                    checkpoint="architecture_replan",
                )
                self.ledger.record_review_recovery(run_id, milestone_id, recovery)
                return self._project_result(
                    run_id,
                    milestone_id,
                    status=(
                        LifecycleStatus.CONTINUE_WITH_REPLAN.value
                        if recovery.outcome is RecoveryOutcome.CONTINUE_WITH_REPLAN
                        else LifecycleStatus.NEEDS_DECISION.value
                    ),
                    accepted=False,
                    reviews=tuple(reviews),
                    repairs=(),
                    recovery=recovery,
                    authority_plan=plan,
                    rendered_evidence=tuple(evidence),
                )
            recovery = self.classify_recovery(checkpoint="architecture_replan")
            self.ledger.record_review_recovery(run_id, milestone_id, recovery)
            self.ledger.record_review_fact(
                run_id,
                milestone_id,
                phase=LifecyclePhase.RECOVERY,
                kind="architecture_replan_accepted",
                data={"finding_id": finding.finding_id, "strategy": proposal.strategy, "boundary_unchanged": True},
            )

        if active_budget.repairs >= active_budget.max_repairs:
            return budget_failure("repairs")
        active_budget = replace(active_budget, repairs=active_budget.repairs + 1)
        finding = blockers[0]
        try:
            repair_record = repair(finding)
        except Exception as exc:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REPAIR_REQUIRED,
                phase=LifecyclePhase.RECOVERY,
                kind="repair_failed",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.EXECUTION_FAILURE,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.FAILED.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=self.classify_recovery(proven_infeasible=True, checkpoint="repair_failed"),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        if repair_record.finding_id != finding.finding_id or not repair_record.same_owner:
            raise ControllerError("multi-authority repair must retain the exact finding and same owner")
        repairs.append(repair_record)
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.RUNNING,
            expected_state=WorkflowState.REPAIR_REQUIRED,
            phase=LifecyclePhase.REPAIR,
            kind="repair_completed",
            data={"repair_id": repair_record.repair_id, "finding_id": finding.finding_id, "same_owner": True},
            reason=ReasonCode.REVIEW_REJECTED,
        )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.COMPLETED,
            expected_state=WorkflowState.RUNNING,
            phase=LifecyclePhase.EXECUTION,
            kind="repair_execution_completed",
            data={"repair_id": repair_record.repair_id},
        )
        fresh_revision = current_revision()
        if AcceptanceMode.VISUAL in modes:
            try:
                refreshed = render_evidence(fresh_revision)  # type: ignore[misc]
            except Exception as exc:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.FAILED,
                    expected_state=WorkflowState.COMPLETED,
                    phase=LifecyclePhase.RECOVERY,
                    kind="rendered_evidence_failed",
                    data={"exception_class": type(exc).__name__},
                    reason=ReasonCode.EXECUTION_FAILURE,
                )
                return self._project_result(
                    run_id,
                    milestone_id,
                    status=LifecycleStatus.FAILED.value,
                    accepted=False,
                    reviews=tuple(reviews),
                    repairs=tuple(repairs),
                    recovery=self.classify_recovery(proven_infeasible=True, checkpoint="rendered_evidence"),
                    authority_plan=plan,
                    rendered_evidence=tuple(evidence),
                )
            if refreshed.evidence_id != evidence[0].evidence_id:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.FAILED,
                    expected_state=WorkflowState.COMPLETED,
                    phase=LifecyclePhase.RECOVERY,
                    kind="stale_rendered_evidence",
                    data={"expected_evidence_id": evidence[0].evidence_id},
                    reason=ReasonCode.REVIEW_REJECTED,
                )
                return self._project_result(
                    run_id,
                    milestone_id,
                    status=LifecycleStatus.STALE_REVIEW.value,
                    accepted=False,
                    reviews=tuple(reviews),
                    repairs=tuple(repairs),
                    recovery=self.classify_recovery(checkpoint="rendered_evidence"),
                    authority_plan=plan,
                    rendered_evidence=tuple(evidence),
                )
            evidence[0] = refreshed
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.REVIEWING,
            expected_state=WorkflowState.COMPLETED,
            phase=LifecyclePhase.REVIEW,
            kind="review_round_started",
            data={"round": 2, "revision": fresh_revision, "acceptance_modes": [mode.value for mode in modes]},
        )
        second_reviews: list[ReviewResult] = []
        try:
            for mode in modes:
                callback = callback_map[mode]
                rendered = evidence[0] if evidence else None
                if mode is AcceptanceMode.OBJECTIVE:
                    second = callback(fresh_revision, True)
                elif mode is AcceptanceMode.VISUAL:
                    second = callback(fresh_revision, rendered, True)
                else:
                    try:
                        parameter_count = len(inspect.signature(callback).parameters)
                    except (TypeError, ValueError):
                        parameter_count = 3
                    second = (
                        callback(fresh_revision, rendered, True)
                        if parameter_count >= 3
                        else callback(fresh_revision, True)
                    )
                if second.reviewer_role != plan.for_mode(mode).role or second.acceptance_mode is not mode:
                    raise ControllerError(f"stale authority for fresh {mode.value} review")
                if not second.fresh or not second.read_only or second.reviewed_revision != fresh_revision:
                    raise ControllerError(f"stale fresh {mode.value} review")
                if mode is AcceptanceMode.VISUAL and evidence[0].evidence_id not in second.evidence_ids:
                    raise ControllerError("fresh visual review did not acknowledge fixed rendered evidence")
                prior = next(review for review in first_reviews if review.acceptance_mode is mode)
                if prior.findings and second.prior_review_id != prior.review_id:
                    raise ControllerError("fresh review must reference the exact prior authority review")
                prior_ids = {item.finding_id for item in prior.findings}
                for item in second.findings:
                    if item.finding_id in prior_ids and not item.survives_prior_repair:
                        raise ControllerError("surviving finding must acknowledge the exact prior repair")
                    if item.finding_id not in prior_ids and item.survives_prior_repair:
                        raise ControllerError("new review scope cannot masquerade as a surviving finding")
                second_reviews.append(second)
                self.ledger.record_review_fact(
                    run_id,
                    milestone_id,
                    phase=LifecyclePhase.REVIEW,
                    kind="authority_review",
                    data={"mode": mode.value, "review": self._review_json(second), "round": 2},
                )
        except AuthorityUnavailable as exc:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="route_unavailable",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.ENVIRONMENT_BLOCKED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.ROUTE_UNAVAILABLE.value,
                accepted=False,
                reviews=tuple(reviews + second_reviews),
                repairs=tuple(repairs),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        except ControllerError as exc:
            recovery = self.classify_recovery(checkpoint="stale_authority")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="stale_authority",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.STALE_AUTHORITY.value,
                accepted=False,
                reviews=tuple(reviews + second_reviews),
                repairs=tuple(repairs),
                recovery=recovery,
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        except Exception as exc:
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="review_failed",
                data={"exception_class": type(exc).__name__},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.FAILED.value,
                accepted=False,
                reviews=tuple(reviews + second_reviews),
                repairs=tuple(repairs),
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        reviews.extend(second_reviews)
        remaining = [finding for review in second_reviews for finding in review.promotion_blockers]
        if remaining:
            recovery = self.classify_recovery(proven_infeasible=True, checkpoint="re_review_rejected")
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.FAILED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.RECOVERY,
                kind="re_review_rejected",
                data={"finding_ids": [finding.finding_id for finding in remaining]},
                reason=ReasonCode.REVIEW_REJECTED,
            )
            return self._project_result(
                run_id,
                milestone_id,
                status=LifecycleStatus.REJECTED_AFTER_REPAIR.value,
                accepted=False,
                reviews=tuple(reviews),
                repairs=tuple(repairs),
                recovery=recovery,
                authority_plan=plan,
                rendered_evidence=tuple(evidence),
            )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            WorkflowState.ACCEPTED,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.ACCEPTANCE,
            kind="all_authorities_accepted",
            data={"review_ids": [review.review_id for review in second_reviews], "same_owner_repair": True},
            reason=ReasonCode.TERMINAL_OUTCOME,
        )
        result = self._project_result(
            run_id,
            milestone_id,
            status=LifecycleStatus.ACCEPTED.value,
            accepted=True,
            reviews=tuple(reviews),
            repairs=tuple(repairs),
            recovery=recovery,
            authority_plan=plan,
            rendered_evidence=tuple(evidence),
        )
        if notification is not None:
            try:
                notification(result)
            except Exception as exc:
                self.ledger.record_review_fact(
                    run_id,
                    milestone_id,
                    phase=LifecyclePhase.ACCEPTANCE,
                    kind="notification_failure",
                    data={"exception_class": type(exc).__name__},
                )
                result = replace(
                    result,
                    notification_failure=True,
                    lifecycle=tuple(
                        LifecycleRecord(item.phase, item.kind, item.sequence, thaw_json(item.data))
                        for item in self.ledger.review_lifecycle(run_id, milestone_id)
                    ),
                )
                write_review_artifact(self.repository_root, run_id, str(milestone_id), result)
        return result
