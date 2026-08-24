"""Agent-usable SDK-first workflow controller vertical slice."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

from .artifacts import write_owned_artifact
from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig
from .domain import (
    ControllerCheckpoint,
    ExecutionCapsule,
    ExecutionRecord,
    ExecutionStatus,
    JsonObject,
    MilestoneId,
    ReasonCode,
    ReasoningEffort,
    RunId,
    Sandbox,
    Schema,
    ThreadIdentity,
    TurnObservation,
    ValidationObservation,
    ValidationSpec,
    WorkflowState,
    WorkspaceMode,
)
from .ledger import Ledger, RecordNotFound
from .worktrees import WorktreeManager


class ControllerError(RuntimeError):
    """A controller operation cannot safely continue."""


class ResumeRequired(ControllerError):
    """A durable SDK identity exists and must be resumed explicitly."""


class UncertainPreIdentity(ControllerError):
    """SDK start failed before identity could be durably bound."""


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
    }
    if set(value) != expected:
        raise ValueError(f"capsule keys must be exactly {sorted(expected)!r}")
    validation = value["validation"]
    if not isinstance(validation, Mapping) or set(validation) != {"argv", "timeout_seconds"}:
        raise ValueError("capsule validation must contain argv and timeout_seconds")
    argv = validation["argv"]
    mutable = value["mutable_paths"]
    protected = value["protected_paths"]
    schema = value["output_schema"]
    if not isinstance(argv, list) or not isinstance(mutable, list) or not isinstance(protected, list):
        raise ValueError("capsule path and argv fields must be arrays")
    if not isinstance(schema, dict):
        raise ValueError("capsule output_schema must be an object")
    return ExecutionCapsule(
        int(cast(int, value["capsule_version"])),
        RunId(cast(str, value["run_id"])),
        MilestoneId(cast(str, value["milestone_id"])),
        Path(cast(str, value["repository_root"])),
        WorkspaceMode(cast(str, value["workspace_mode"])),
        Path(cast(str, value["workspace_path"])),
        cast(str, value["branch"]),
        cast(str, value["base_sha"]),
        cast(str, value["lane"]),
        tuple(cast(list[str], mutable)),
        tuple(cast(list[str], protected)),
        ValidationSpec(tuple(cast(list[str], argv)), float(cast(float, validation["timeout_seconds"]))),
        cast(str, value["model"]),
        ReasoningEffort(cast(str, value["reasoning_effort"])),
        cast(str, value["prompt"]),
        cast(JsonObject, schema),
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
    return ValidationObservation(
        spec.argv,
        exit_code,
        _digest_bytes(stdout),
        _digest_bytes(stderr),
        timed_out,
        time.monotonic() - started,
    )


class Controller:
    """Own one durable execution route from capsule to terminal result."""

    def __init__(
        self,
        state_root: Path,
        *,
        adapter_factory: AdapterFactory = _adapter_factory,
        worktrees: WorktreeManager | None = None,
        fault_injector: ControllerFaultInjector | None = None,
    ) -> None:
        if not state_root.is_absolute():
            raise ValueError("controller state root must be absolute")
        self.state_root = state_root
        self.state_dir = state_root / ".codex-flow"
        self.ledger = Ledger(self.state_dir / "workflow.db")
        self._adapter_factory = adapter_factory
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
        source = capsule.workspace_path if capsule.workspace_path.exists() else capsule.repository_root
        protected_before = protected_paths_digest(source, capsule.protected_paths)
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
        if tracked.returncode != 0 or untracked.returncode != 0:
            raise ControllerError("unable to inspect workspace mutation scope")
        return frozenset(
            path
            for path in (
                *(os.fsdecode(item) for item in tracked.stdout.split(b"\0") if item),
                *(os.fsdecode(item) for item in untracked.stdout.split(b"\0") if item),
            )
            if path and path != ".codex-flow" and not path.startswith(".codex-flow/")
        )

    @staticmethod
    def _path_is_owned(path: str, roots: tuple[str, ...]) -> bool:
        candidate = Path(path)
        return any(candidate == Path(root) or Path(root) in candidate.parents for root in roots)

    def _assert_mutation_scope(self, capsule: ExecutionCapsule) -> None:
        outside = sorted(
            path
            for path in self._workspace_changes(capsule.workspace_path)
            if not self._path_is_owned(path, capsule.mutable_paths)
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
        self.ledger.acquire_workspace_lease(capsule)
        self._assert_protected_clean(capsule)
        self._assert_mutation_scope(capsule)
        current_digest = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
        if current_digest != record.protected_before_sha256:
            raise ControllerError("protected paths changed between plan and start")
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=capsule.workspace_path,
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
            self.ledger.transition(
                capsule.run_id,
                capsule.milestone_id,
                WorkflowState.RUNNING,
                expected_state=WorkflowState.STARTING,
            )
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
        self.ledger.acquire_workspace_lease(capsule)
        adapter = self._adapter_factory(
            CodexSdkConfig(
                capsule.model,
                capsule.reasoning_effort,
                sandbox=Sandbox.WORKSPACE_WRITE,
                cwd=capsule.workspace_path,
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
        if record.turn_id is None:
            observation = adapter.run_turn(record.thread_id, capsule.prompt, output_schema=capsule.output_schema)
            record = self.ledger.record_turn(capsule.run_id, capsule.milestone_id, observation)
            self._fault("after_turn")
        turn_output = record.turn_output or {}
        validation = _run_validation(capsule.validation, capsule.workspace_path)
        protected_after = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
        result: JsonObject = turn_output
        mutation_scope_valid = True
        try:
            self._assert_mutation_scope(capsule)
        except ControllerError:
            mutation_scope_valid = False
        if protected_after != record.protected_before_sha256 or not mutation_scope_valid:
            validation = ValidationObservation(
                validation.argv,
                validation.exit_code if validation.exit_code != 0 else 125,
                validation.stdout_sha256,
                validation.stderr_sha256,
                validation.timed_out,
                validation.duration_seconds,
            )
            result = {
                "status": "failed",
                "reason": (
                    "protected_paths_changed"
                    if protected_after != record.protected_before_sha256
                    else "mutation_outside_owned_paths"
                ),
            }
        terminal = self.ledger.record_terminal_execution(
            capsule.run_id,
            capsule.milestone_id,
            result=result,
            validation=validation,
            protected_after_sha256=protected_after,
        )
        target = WorkflowState.COMPLETED if terminal.status is ExecutionStatus.COMPLETED else WorkflowState.FAILED
        self.ledger.transition(
            capsule.run_id,
            capsule.milestone_id,
            target,
            expected_state=WorkflowState.RUNNING,
            reason=ReasonCode.TERMINAL_OUTCOME if target is WorkflowState.COMPLETED else ReasonCode.EXECUTION_FAILURE,
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
        payload = {
            "run_id": str(record.run_id),
            "milestone_id": str(record.milestone_id),
            "status": record.status.value,
            "checkpoint": record.checkpoint.value,
            "workspace_path": str(record.workspace_path),
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
        record = self.ledger.cancel_execution(run_id, milestone_id)
        try:
            state = self.ledger.current_state(run_id, milestone_id)
        except RecordNotFound:
            return record
        if state not in {
            WorkflowState.ACCEPTED,
            WorkflowState.BLOCKED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }:
            self.ledger.transition(
                run_id,
                milestone_id,
                WorkflowState.CANCELLED,
                expected_state=state,
            )
        return self.ledger.get_execution(run_id, milestone_id)


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
            }
            if record.validation
            else None
        ),
        "protected_before_sha256": record.protected_before_sha256,
        "protected_after_sha256": record.protected_after_sha256,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
