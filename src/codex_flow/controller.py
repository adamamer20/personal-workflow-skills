"""Agent-usable SDK-first workflow controller vertical slice."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .artifacts import write_owned_artifact
from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig, NativeRuntimeConfig
from .domain import (
    ControllerCheckpoint,
    ExecutionCapsule,
    ExecutionRecord,
    ExecutionStatus,
    JsonObject,
    MilestoneId,
    NativePermissionAuthority,
    NativePermissionMode,
    ReasonCode,
    ReasoningEffort,
    RunId,
    Schema,
    ThreadIdentity,
    TurnObservation,
    ValidationFailureCode,
    ValidationObservation,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
    canonicalize_execution_capsule,
    strict_json_loads,
    thaw_json,
)
from .ledger import Ledger, NativeCompatibilityConflict, NativePermissionConflict
from .native_profile import NativeDiscoveryCompatibilityError, NativeProfileProjection
from .worktrees import WorktreeManager


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
        self, thread: ThreadIdentity, input: str, *, output_schema: Schema | None = None
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


def _stable_git_tree_digest(paths: tuple[Path, ...]) -> tuple[str, int, int]:
    """Hash selected stable metadata, excluding volatile lock files."""

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
                directories[:] = sorted(name for name in directories if not name.endswith(".lock"))
                for name in sorted(item for item in names if not item.endswith(".lock")):
                    path = Path(current) / name
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
    refs = _bounded_command(
        workspace,
        ("git", "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"),
    )
    selected = tuple(
        dict.fromkeys(
            (
                common_dir / "config",
                common_dir / "config.worktree",
                common_dir / "packed-refs",
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
    metadata_sha256, file_count, byte_count = _stable_git_tree_digest(selected)
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
    return {
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
            "timeout_seconds": capsule.validation.timeout_seconds,
        },
        "model": capsule.model,
        "reasoning_effort": capsule.reasoning_effort.value,
        "prompt": capsule.prompt,
        "output_schema": thaw_json(capsule.output_schema),
        "permission_mode": capsule.permission_mode.value,
    }


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
    if set(value) != expected:
        raise ValueError(f"capsule keys must be exactly {sorted(expected)!r}")

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
    run_id = require_string("run_id")
    milestone_id = require_string("milestone_id")
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
        target = root / relative
        digest.update(relative.encode())
        if not target.exists():
            digest.update(b"\0missing\0")
            continue
        if target.is_symlink():
            raise ControllerError(f"protected path is a symlink: {target}")
        candidates = (
            (target,) if target.is_file() else tuple(sorted(item for item in target.rglob("*") if item.is_file()))
        )
        for candidate in candidates:
            if candidate.is_symlink():
                raise ControllerError(f"protected path contains a symlink: {candidate}")
            digest.update(str(candidate.relative_to(root)).encode())
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
        if self.state_root != repository_toplevel:
            raise ControllerError("controller state root must equal the execution repository toplevel")
        protected_before = protected_paths_digest_at_revision(
            capsule.repository_root, capsule.base_sha, capsule.protected_paths
        )
        content = _canonical_json(capsule_json(capsule))
        capsule_path = self._capsule_path(capsule)
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
        ignored = subprocess.run(
            ("git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if tracked.returncode != 0 or untracked.returncode != 0 or ignored.returncode != 0:
            raise ControllerError("unable to inspect workspace mutation scope")
        return frozenset(
            path
            for path in (
                *(os.fsdecode(item) for item in tracked.stdout.split(b"\0") if item),
                *(os.fsdecode(item) for item in untracked.stdout.split(b"\0") if item),
                *(os.fsdecode(item) for item in ignored.stdout.split(b"\0") if item),
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
                entries_seen += 1
                if entries_seen > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise ControllerError("workspace directory topology exceeds the entry limit")
                path = Path(entry.path)
                relative = path.relative_to(workspace).as_posix()
                if len(os.fsencode(relative)) > _MAX_WORKSPACE_PATH_BYTES:
                    raise ControllerError("workspace directory topology contains an overlong path")
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

    @staticmethod
    def _path_signature(path: Path) -> str:
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
            facts.append((relative, self._path_signature(capsule.workspace_path / relative)))
        by_path = dict(facts)
        for relative, signature in self._directory_topology_changes(capsule, baseline_head):
            by_path.setdefault(relative, signature)
        return tuple(sorted(by_path.items()))

    def _owned_workspace_snapshot(self, capsule: ExecutionCapsule) -> tuple[tuple[str, str], ...]:
        workspace_device = os.stat(capsule.workspace_path).st_dev
        facts: list[tuple[str, str]] = []
        for relative in sorted(capsule.mutable_paths):
            self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            self._assert_safe_tree(capsule.workspace_path / relative, workspace_device)
            facts.append((relative, self._path_signature(capsule.workspace_path / relative)))
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

    @staticmethod
    def _assert_safe_tree(root: Path, workspace_device: int) -> None:
        """Reject symlink traversal and multiply-linked files in mutable roots."""

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
                entries_seen += 1
                if entries_seen > _MAX_WORKSPACE_SCAN_ENTRIES:
                    raise ControllerError(f"mutable path exceeds the entry limit: {root}")
                child = Path(entry.path)
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
        for relative in capsule.mutable_paths:
            self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            self._assert_safe_tree(capsule.workspace_path / relative, workspace_device)
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

    def _authorize_external_call(self, capsule: ExecutionCapsule, record: ExecutionRecord) -> None:
        """Exactly revalidate every durable authority immediately before an external boundary."""

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
                self._authorize_external_call(capsule, record)
            except Exception:
                self.ledger._reject_thread_start_authorization(capsule.run_id, capsule.milestone_id)
                raise
            try:
                identity = adapter.start_thread()
            except Exception as exc:
                self.ledger.record_pre_identity_uncertainty(capsule.run_id, capsule.milestone_id)
                raise UncertainPreIdentity(
                    "SDK start failed before durable identity; automatic retry is forbidden"
                ) from exc
            record = self.ledger.record_thread_identity(capsule.run_id, capsule.milestone_id, identity)
            self._fault("after_thread_identity")
            return self._execute_turn_and_finish(capsule, record, adapter)
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
            self._authorize_external_call(capsule, record)
            resumed = adapter.resume_thread(record.thread_id)
            if resumed != record.thread_id:
                raise ControllerError("SDK resume changed durable thread identity")
            return self._execute_turn_and_finish(capsule, record, adapter)
        finally:
            adapter.close()

    def _execute_turn_and_finish(
        self, capsule: ExecutionCapsule, record: ExecutionRecord, adapter: Adapter | None
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
            observation = adapter.run_turn(record.thread_id, capsule.prompt, output_schema=capsule.output_schema)
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
