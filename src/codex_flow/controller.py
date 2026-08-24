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
)
from .ledger import Ledger
from .native_profile import NativeProfileProjection
from .worktrees import WorktreeManager


class ControllerError(RuntimeError):
    """A controller operation cannot safely continue."""


class ResumeRequired(ControllerError):
    """A durable SDK identity exists and must be resumed explicitly."""


class UncertainPreIdentity(ControllerError):
    """SDK start failed before identity could be durably bound."""


class UncertainTurn(ControllerError):
    """An external SDK turn may have happened without a durable response."""


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
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


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
        "output_schema": capsule.output_schema,
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
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
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
        if not state_root.is_absolute():
            raise ValueError("controller state root must be absolute")
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

    def _native_runtime(self, capsule: ExecutionCapsule) -> tuple[NativeRuntimeConfig | None, str]:
        runtime_root = self.state_dir / "sdk-runtime" / str(capsule.run_id) / str(capsule.milestone_id)
        global_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
        native_profile = (
            NativeProfileProjection.load(global_home) if self._production_adapter else self._trusted_test_native_profile
        )
        runtime = NativeRuntimeConfig(runtime_root / "home", native_profile) if native_profile is not None else None
        facts: JsonObject = {
            "schema": "codex-flow/native-runtime-profile/v1",
            "workspace": os.fspath(capsule.workspace_path),
            "effective_permissions": (
                native_profile.effective_permissions(capsule.permission_mode)
                if native_profile is not None
                else {"test_seam": True}
            ),
            "runtime_home": os.fspath(runtime.runtime_home) if runtime is not None else None,
            "native_profile": native_profile.sanitized_facts if native_profile is not None else {"test_seam": True},
            "transport": "openai-codex-sdk-app-server",
        }
        return runtime, _digest_bytes(_canonical_json(facts))

    def _committed_workspace_paths(self, capsule: ExecutionCapsule) -> frozenset[str]:
        """Return every path touched by every commit after the capsule base.

        Comparing only the final tree to ``base_sha`` misses an out-of-scope
        commit that is subsequently reverted.  The commit walk is therefore a
        separate integrity fact from the final-tree diff.
        """

        commits = self._git_output_bytes(
            capsule.workspace_path,
            "rev-list",
            "--reverse",
            "--ancestry-path",
            f"{capsule.base_sha}..HEAD",
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
        final_tree = self._git_output_bytes(capsule.workspace_path, "diff", "--name-only", "-z", capsule.base_sha, "--")
        paths.update(os.fsdecode(item) for item in final_tree.split(b"\0") if item)
        return frozenset(paths)

    def _assert_workspace_history(self, capsule: ExecutionCapsule) -> None:
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
        paths = self._committed_workspace_paths(capsule)
        outside = sorted(path for path in paths if not self._path_is_owned(path, capsule.mutable_paths))
        if outside:
            raise ControllerError("committed workspace changes outside mutable paths: " + ", ".join(outside))

    def _workspace_changes(self, workspace: Path) -> frozenset[str]:
        tracked = subprocess.run(
            ("git", "diff", "--name-only", "-z", "HEAD"),
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

    @staticmethod
    def _is_controller_path(path: str) -> bool:
        candidate = Path(path)
        internal = Path(".codex-flow")
        return candidate == internal or internal in candidate.parents

    @staticmethod
    def _assert_safe_tree(root: Path, workspace_device: int) -> None:
        """Reject symlink traversal and multiply-linked files in mutable roots."""

        try:
            metadata = os.lstat(root)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ControllerError(f"mutable path is a symlink: {root}")
        if metadata.st_dev != workspace_device:
            raise ControllerError(f"mutable path is outside the workspace device: {root}")
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise ControllerError(f"mutable file has external hardlinks: {root}")
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ControllerError(f"mutable path is not a regular file or directory: {root}")
        try:
            with os.scandir(root) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ControllerError(f"unable to inspect mutable path: {root}") from exc
        for entry in entries:
            Controller._assert_safe_tree(Path(entry.path), workspace_device)

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

    def _assert_mutation_scope(self, capsule: ExecutionCapsule) -> None:
        changes = self._workspace_changes(capsule.workspace_path)
        workspace_device = os.stat(capsule.workspace_path).st_dev
        for relative in capsule.mutable_paths:
            self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
            self._assert_safe_tree(capsule.workspace_path / relative, workspace_device)
        for relative in changes:
            if not self._is_controller_path(relative):
                self._assert_safe_changed_path(capsule.workspace_path, relative, workspace_device)
        outside = sorted(
            path
            for path in changes
            if not self._is_controller_path(path) and not self._path_is_owned(path, capsule.mutable_paths)
        )
        if outside:
            raise ControllerError(f"workspace contains changes outside mutable paths: {', '.join(outside)}")

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
        self.ledger.claim_dispatch(capsule.run_id, capsule.milestone_id, "executor", 1)
        self._worktrees.select(capsule)
        self._assert_protected_clean(capsule)
        self._assert_workspace_history(capsule)
        self._assert_mutation_scope(capsule)
        current_digest = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
        if current_digest != record.protected_before_sha256:
            raise ControllerError("protected paths changed between plan and start")
        self.ledger.acquire_workspace_lease(capsule)
        native_runtime, native_profile_sha256 = self._native_runtime(capsule)
        self.ledger.record_native_profile(capsule.run_id, capsule.milestone_id, native_profile_sha256)
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=None,
                cwd=capsule.workspace_path,
                native_runtime=native_runtime,
                permission_mode=(capsule.permission_mode if native_runtime is not None else None),
            )
        )
        try:
            try:
                self.ledger.record_thread_starting(capsule.run_id, capsule.milestone_id)
                self._fault("before_sdk_thread_start")
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
        self._assert_workspace_history(capsule)
        self.ledger.acquire_workspace_lease(capsule)
        native_runtime, native_profile_sha256 = self._native_runtime(capsule)
        self.ledger.record_native_profile(capsule.run_id, capsule.milestone_id, native_profile_sha256)
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=None,
                cwd=capsule.workspace_path,
                native_runtime=native_runtime,
                permission_mode=(capsule.permission_mode if native_runtime is not None else None),
            )
        )
        try:
            resumed = adapter.resume_thread(record.thread_id)
            if resumed != record.thread_id:
                raise ControllerError("SDK resume changed durable thread identity")
            return self._execute_turn_and_finish(capsule, record, adapter)
        finally:
            adapter.close()

    def _execute_turn_and_finish(
        self, capsule: ExecutionCapsule, record: ExecutionRecord, adapter: Adapter
    ) -> ExecutionRecord:
        if record.thread_id is None:
            raise ControllerError("cannot execute without a durable SDK identity")
        if record.turn_id is None and record.turn_output == {"__controller_checkpoint": "turn_starting"}:
            raise UncertainTurn("SDK turn crossed the external-call boundary without a durable response")
        controller_state_error = ""
        integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        if record.turn_id is None:
            git_before = git_authority_snapshot(capsule.workspace_path)
            integrity = self.ledger.record_git_authority_before(
                capsule.run_id,
                capsule.milestone_id,
                git_before.sha256,
            )
            self.ledger.record_turn_starting(capsule.run_id, capsule.milestone_id)
            controller_state_before = self._controller_tree_snapshot(self.state_root)
            self._fault("before_sdk_turn")
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
            self._assert_workspace_history(capsule)
            self._assert_mutation_scope(capsule)
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
        terminal = self.ledger.record_terminal_execution(
            capsule.run_id,
            capsule.milestone_id,
            result=result,
            validation=validation,
            protected_after_sha256=protected_after,
            git_authority_after_sha256=git_after_sha256,
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
            "git_authority_before_sha256": integrity.git_authority_before_sha256 if integrity else None,
            "git_authority_after_sha256": integrity.git_authority_after_sha256 if integrity else None,
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
