"""Out-of-process model workers and the direct result submission client."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .backends.codex_sdk import (
    LEAF_WORKER_ALLOWED_OPERATIONS,
    LEAF_WORKER_CONFIG_OVERRIDES,
    CodexSdkAdapter,
    CodexSdkConfig,
    LiveTurnHandle,
    NativeRuntimeConfig,
    ResponseChainInvalid,
    SchemaOutputInvalid,
)
from .contracts import (
    ModelFacingResult,
    PluginCapabilitySnapshot,
    PluginRequirement,
    model_facing_review_result_schema_sha256,
    review_result_from_agent_message,
)
from .domain import (
    AuthenticationFailure,
    ConversationHistoryRequest,
    ConversationSubjectKind,
    IntegrityFailure,
    LifecycleEvent,
    LocalImageInput,
    MalformedInputFailure,
    NativePermissionAuthority,
    NativePermissionMode,
    PermissionFailure,
    ProfileFailure,
    ReasoningEffort,
    Sandbox,
    Schema,
    SkillInput,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    TransientFailureAfterIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    UnknownSdkFailureAfterIdentity,
    UnknownSdkFailureBeforeIdentity,
    UnsupportedCapability,
    WorkerResultRejectionCode,
    is_lossy_worker_diagnostic_method,
    redact_diagnostic_text,
    strict_json_loads,
    validate_local_image_inputs,
)
from .ipc import IpcError, IpcTransportError, ensure_runtime_dir, send_request
from .native_profile import NativeProfileProjection
from .plugin_capabilities import PluginCapabilityError, _verified_skill_input, standard_codex_home


class WorkerError(RuntimeError):
    """A worker could not complete its bounded execution protocol."""


class ResultTransportAfterIdentity(WorkerError):
    """Bounded local result-submit recovery exhausted after SDK identity."""


WORKER_EXIT_RESPONSE_CHAIN_INVALID = 75
WORKER_EXIT_TERMINAL_AFTER_IDENTITY = 76
WORKER_EXIT_SCHEMA_OUTPUT_INVALID = 77
WORKER_EXIT_TRANSPORT_BEFORE_IDENTITY = 74
# Generic worker protocol/capability failures fail closed and are never
# interpreted as a recoverable provider or process-loss retry.
WORKER_EXIT_FAIL_CLOSED = 78
WORKER_EXIT_TRANSIENT_AFTER_IDENTITY = 79
WORKER_EXIT_AUTHENTICATION = 80
WORKER_EXIT_PERMISSION = 81
WORKER_EXIT_CAPABILITY = 82
WORKER_EXIT_PROFILE = 83
WORKER_EXIT_INTEGRITY = 84
WORKER_EXIT_MALFORMED = 85
WORKER_EXIT_UNKNOWN_AFTER_IDENTITY = 86
WORKER_EXIT_UNKNOWN_BEFORE_IDENTITY = 87
WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY = 88
SCHEMA_CORRECTION_BUDGET = 2
RESULT_SUBMIT_MAX_ATTEMPTS = 3
RESULT_SUBMIT_MAX_SECONDS = 0.25
RESULT_SUBMIT_BACKOFF_SECONDS = 0.01
_RETRYABLE_RESULT_SUBMIT_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EINTR,
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ENOTCONN,
        errno.ECONNREFUSED,
    }
)
RECOVERY_CONTINUATION_PREAMBLE = (
    "RECOVERY INSPECT-BEFORE-MUTATE: this is a bounded recovery execution. "
    "The assigned worktree retains changes made by the prior worker. Inspect the current workspace and retained "
    "evidence before any mutation. The controller already established this capability as the sole mutable worker; "
    "the supervisor, this worker process, and this dispatch's active row are expected parts of your own attempt, not "
    "conflicting owners. Do not inspect codex-flow runtime ownership or declare blocked merely because your own "
    "attempt is active. Do not repeat repository work already present, do not delegate, and return exactly one "
    "schema-v1 ModelFacingResult terminal envelope."
)


def recovery_continuation_prompt(
    *,
    workspace: Path,
    resume_same_thread: bool,
    original_prompt: str,
    observed_state: str = "idle",
) -> str:
    """Build the bounded, inspect-before-mutate continuation instruction."""

    if not workspace.is_absolute() or not workspace.exists():
        raise WorkerError("recovery workspace is unavailable")
    if not observed_state or len(observed_state) > 128 or "\x00" in observed_state:
        raise WorkerError("recovery observation fact is invalid")
    if (
        not isinstance(original_prompt, str)
        or not original_prompt.strip()
        or "\x00" in original_prompt
        or len(original_prompt.encode("utf-8")) > 2_000_000
    ):
        raise WorkerError("original recovery prompt is invalid")
    thread_instruction = (
        "Resume the existing SDK thread and use its retained context."
        if resume_same_thread
        else "This is a fresh SDK thread replacing a failed execution; recover context from the worktree, not an assumed transcript."
    )
    return (
        f"{RECOVERY_CONTINUATION_PREAMBLE} {thread_instruction} "
        f"Workspace: {workspace}. Observed state: {observed_state}.\n\n"
        f"Original durable milestone prompt follows unchanged:\n{original_prompt}"
    )


def _resolve_shared_native_home(configured_home: str | None, *, fallback_home: Path) -> Path:
    """Resolve the standard Codex home without requiring an exported override."""

    candidate = Path(configured_home) if configured_home else fallback_home / ".codex"
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkerError("shared native Codex home is unavailable") from exc


def _run_bounded_schema_turn(
    adapter: CodexSdkAdapter,
    thread: ThreadIdentity,
    prompt: str | SkillInput,
    output_schema: Schema | None,
    *,
    local_image_inputs: tuple[LocalImageInput, ...] = (),
    event_callback: Callable[[LifecycleEvent], None] | None = None,
    turn_callback: Callable[[str], None] | None = None,
) -> TurnObservation:
    """Run a turn and repair only its terminal envelope at most twice."""

    turn_prompt = prompt
    for correction in range(SCHEMA_CORRECTION_BUDGET + 1):
        try:
            return adapter.run_turn(
                thread,
                turn_prompt,
                local_image_inputs=local_image_inputs,
                output_schema=output_schema,
                event_callback=event_callback,
                turn_callback=turn_callback,
            )
        except SchemaOutputInvalid as exc:
            if correction >= SCHEMA_CORRECTION_BUDGET:
                raise
            violation = " ".join(str(exc).split())[:256] or "schema validation failed"
            turn_prompt = (
                "Your previous terminal response was rejected by the controller-owned output schema. "
                "Do not repeat repository work, modify files, run tests, delegate, or add commentary. "
                "Return only one corrected terminal JSON object matching the unchanged schema. "
                f"Validator violation: {violation}"
            )
    raise WorkerError("schema correction budget accounting failed")  # pragma: no cover


NESTED_LIFECYCLE_OPERATIONS = frozenset(
    {
        "collaboration.spawn",
        "collaboration.followup",
        "collaboration.wait",
        "collaboration.message",
        "collaboration.interrupt",
        "collaboration.list",
        "app.create",
        "app.fork",
        "app.send",
        "app.wait",
        "app.handoff",
    }
)


def enforce_leaf_operation(operation: str) -> None:
    """Reject nested task/App lifecycle attempts from a leaf worker.

    This is a defense-in-depth guard for worker-side adapters and adversarial
    sentinels.  The actual model tool surface is removed by the SDK config
    overrides before thread start; prompt prose is never treated as policy.
    """

    if operation in NESTED_LIFECYCLE_OPERATIONS or operation not in LEAF_WORKER_ALLOWED_OPERATIONS:
        raise WorkerError(f"leaf worker operation is not permitted: {operation}")


def run_nested_delegation_sentinel() -> dict[str, object]:
    """Adversarially exercise every nested lifecycle operation.

    The sentinel intentionally attempts each operation through the same
    worker-side guard and records only booleans/counts.  It never calls a
    collaboration or App API, so a passing result proves no child task,
    lifecycle mutation, or hidden successor was created by the worker.
    """

    blocked: list[str] = []
    for operation in sorted(NESTED_LIFECYCLE_OPERATIONS):
        try:
            enforce_leaf_operation(operation)
        except WorkerError:
            blocked.append(operation)
    return {
        "worker_role": "leaf",
        "attempted": len(NESTED_LIFECYCLE_OPERATIONS),
        "blocked": len(blocked),
        "child_created": False,
        "lifecycle_mutations": 0,
        "hidden_successors": 0,
        "operations": blocked,
    }


def _leaf_policy(capability: dict[str, object]) -> bool:
    return (
        capability.get("worker_role") == "leaf"
        and capability.get("allowed_operations") == ["submit_result"]
        and capability.get("leaf_worker_policy")
        == {
            "agents.enabled": False,
            "config_overrides": list(LEAF_WORKER_CONFIG_OVERRIDES),
            "features.multi_agent": False,
        }
    )


_PRIVATE_CAPABILITY_KEYS = frozenset(
    {
        "version",
        "operation",
        "dispatch_id",
        "generation",
        "attempt",
        "backend",
        "workspace_path",
        "schema_sha256",
        "token",
        "socket_path",
        "worker_role",
        "allowed_operations",
        "leaf_worker_policy",
        "effective_permission",
        "native_profile_sha256",
        "native_compatibility_sha256",
        "plugin_requirements",
        "plugin_capabilities",
    }
)


@dataclass(frozen=True, slots=True)
class _PrivateWorkerCapability:
    """One exact controller-issued capability and its typed plugin facts."""

    payload: dict[str, object]
    plugin_requirements: tuple[PluginRequirement, ...]
    plugin_snapshots: tuple[PluginCapabilitySnapshot, ...]


def _parse_private_plugin_authority(
    capability: dict[str, object],
) -> tuple[tuple[PluginRequirement, ...], tuple[PluginCapabilitySnapshot, ...]]:
    raw_requirements = capability["plugin_requirements"]
    raw_capabilities = capability["plugin_capabilities"]
    if not isinstance(raw_requirements, list) or not isinstance(raw_capabilities, list):
        raise WorkerError("worker plugin capability authority is malformed")
    try:
        requirements = tuple(PluginRequirement.from_json(item) for item in raw_requirements)
        snapshots: list[PluginCapabilitySnapshot] = []
        for raw_snapshot in raw_capabilities:
            if not isinstance(raw_snapshot, dict) or not isinstance(raw_snapshot.get("capability_sha256"), str):
                raise ValueError("worker plugin capability digest is malformed")
            snapshot = PluginCapabilitySnapshot.from_json(
                {key: value for key, value in raw_snapshot.items() if key != "capability_sha256"}
            )
            if raw_snapshot["capability_sha256"] != snapshot.capability_digest:
                raise ValueError("worker plugin capability digest does not match its snapshot")
            snapshots.append(snapshot)
        if len(snapshots) != len(requirements) or any(
            not snapshot.satisfies(requirement) for requirement, snapshot in zip(requirements, snapshots, strict=True)
        ):
            raise ValueError("worker plugin capability facts do not satisfy their requirements")
    except (TypeError, ValueError) as exc:
        raise WorkerError("worker plugin capability authority is invalid") from exc
    return requirements, tuple(snapshots)


def _read_private_capability(path: Path) -> _PrivateWorkerCapability:
    raw = _read_private_file(path, max_bytes=8_192, require_readonly=True)
    try:
        value = strict_json_loads(raw, max_bytes=8_192)
    except ValueError as exc:
        raise WorkerError("capability file is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise WorkerError("capability file root must be an object")
    capability = value
    if (
        set(capability) != _PRIVATE_CAPABILITY_KEYS
        or capability.get("version") != 1
        or capability.get("operation") != "submit_result"
    ):
        raise WorkerError("capability file has an unsupported closed shape")
    if not _leaf_policy(capability):
        raise WorkerError("worker capability is not bound to the leaf-worker policy")
    token = capability.get("token")
    if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise WorkerError("capability token is invalid")
    requirements, snapshots = _parse_private_plugin_authority(capability)
    return _PrivateWorkerCapability(capability, requirements, snapshots)


def _read_object(path: Path, *, max_bytes: int = 131_072) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        value = strict_json_loads(raw, max_bytes=max_bytes)
    except (OSError, ValueError) as exc:
        raise WorkerError("worker input is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise WorkerError("worker input root must be an object")
    return value


def _read_private_file(path: Path, *, max_bytes: int, require_readonly: bool = False) -> bytes:
    """Read a controller-provided private file without following links."""

    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise WorkerError("worker private input is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (require_readonly and stat.S_IMODE(metadata.st_mode) != 0o400)
    ):
        raise WorkerError("worker private input path is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkerError("worker private input cannot be read") from exc
    if len(raw) > max_bytes:
        raise WorkerError("worker private input exceeds its bound")
    return raw


def _write_once(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise WorkerError("worker private output write was incomplete")
        view = view[written:]


def submit_result(
    *,
    capability_file: Path,
    result_file: Path,
    socket_path: Path | None = None,
) -> dict[str, object]:
    """Submit one complete raw result using a controller-issued capability."""

    return _submit_result(
        capability_file=capability_file,
        result_file=result_file,
        socket_path=socket_path,
        expected_capability=None,
    )


def _submit_result(
    *,
    capability_file: Path,
    result_file: Path,
    socket_path: Path | None,
    expected_capability: _PrivateWorkerCapability | None,
) -> dict[str, object]:
    """Submit through one freshly reread capability bound to worker-start facts."""

    enforce_leaf_operation("submit_result")
    binding = _read_private_capability(capability_file)
    if expected_capability is not None and binding != expected_capability:
        raise WorkerError("private worker capability changed before result submission")
    capability = binding.payload
    token = cast(str, capability["token"])
    raw = _read_private_file(result_file, max_bytes=65_536)
    # Parse locally before sending, but preserve exact bytes for the digest and
    # controller-owned strict parser.
    if str(capability["schema_sha256"]) == model_facing_review_result_schema_sha256():
        review_result_from_agent_message(raw)
    else:
        ModelFacingResult.from_agent_message(raw)
    endpoint = socket_path or Path(str(capability["socket_path"]))
    request = {
        "version": 1,
        "operation": "submit_result",
        "dispatch_id": capability["dispatch_id"],
        "generation": capability["generation"],
        "attempt": capability["attempt"],
        "backend": capability["backend"],
        "workspace_path": capability["workspace_path"],
        "schema_sha256": capability["schema_sha256"],
        "token": token,
        "raw_result": raw.decode("utf-8"),
    }
    response: dict[str, object] | None = None
    last_transport_error: BaseException | None = None
    deadline = time.monotonic() + RESULT_SUBMIT_MAX_SECONDS
    for submit_attempt in range(RESULT_SUBMIT_MAX_ATTEMPTS):
        try:
            response = send_request(endpoint, request)
            break
        except (IpcTransportError, OSError) as exc:
            retryable = (
                isinstance(exc, IpcTransportError)
                or isinstance(exc, TimeoutError | ConnectionError)
                or getattr(exc, "errno", None) in _RETRYABLE_RESULT_SUBMIT_ERRNOS
            )
            if not retryable:
                raise WorkerError("worker result submission failed") from exc
            last_transport_error = exc
            if submit_attempt + 1 >= RESULT_SUBMIT_MAX_ATTEMPTS:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(RESULT_SUBMIT_BACKOFF_SECONDS * (2**submit_attempt), remaining)
            if delay > 0:
                time.sleep(delay)
        except IpcError as exc:
            # Protocol, endpoint-safety and malformed-frame failures are
            # integrity failures, never transient result transport.
            raise WorkerError("worker result submission failed") from exc
    if response is None:
        raise ResultTransportAfterIdentity("worker result transport recovery exhausted after identity") from (
            last_transport_error
        )
    if not isinstance(response, dict):
        raise WorkerError("controller returned an invalid worker acknowledgement")
    if response.get("ok") is False:
        reason_code = response.get("reason_code")
        allowed_codes = {item.value for item in WorkerResultRejectionCode}
        if isinstance(reason_code, str) and reason_code in allowed_codes:
            raise WorkerError(f"worker result rejected: {reason_code}")
        raise WorkerError("worker result rejected")
    if response.get("version") != 1 or response.get("dispatch_id") != capability["dispatch_id"]:
        raise WorkerError("controller returned an invalid worker acknowledgement")
    return response


def write_capability(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    if len(encoded) > 8_192:
        raise WorkerError("capability descriptor is oversized")
    ensure_runtime_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o400)
    try:
        _write_once(fd, encoded)
    finally:
        os.close(fd)


def run_sdk_worker(
    *,
    capability_file: Path,
    capsule_file: Path,
    result_file: Path,
    socket_path: Path,
    resume_thread_id: str | None = None,
) -> dict[str, object]:
    """Run one SDK-headless turn outside the supervisor process."""

    capability_binding = _read_private_capability(capability_file)
    capability = capability_binding.payload
    capsule_raw = _read_private_file(capsule_file, max_bytes=2_000_000, require_readonly=True)
    try:
        capsule_value = strict_json_loads(capsule_raw, max_bytes=2_000_000)
    except ValueError as exc:
        raise WorkerError("worker capsule is not valid bounded JSON") from exc
    if not isinstance(capsule_value, dict):
        raise WorkerError("worker capsule root must be an object")
    capsule = capsule_value
    required = {"model", "reasoning_effort", "prompt", "workspace_path"}
    if not required.issubset(capsule):
        raise WorkerError("worker capsule lacks SDK execution facts")
    try:
        raw_requirements = capsule.get("plugin_requirements", [])
        if raw_requirements != [item.to_json() for item in capability_binding.plugin_requirements] or not isinstance(
            raw_requirements, list
        ):
            raise ValueError("worker plugin requirements do not match its private capability")
        plugin_requirements = tuple(PluginRequirement.from_json(item) for item in raw_requirements)
        plugin_snapshots = capability_binding.plugin_snapshots
    except (PluginCapabilityError, TypeError, ValueError) as exc:
        raise WorkerError("worker plugin capability authority is invalid") from exc
    workspace = Path(str(capsule["workspace_path"])).resolve(strict=True)
    raw_images = capsule.get("local_image_paths", [])
    if not isinstance(raw_images, list) or any(not isinstance(item, str) for item in raw_images):
        raise WorkerError("worker local image paths are malformed")
    try:
        local_image_inputs = validate_local_image_inputs(raw_images, workspace=workspace) if raw_images else ()
    except (OSError, ValueError) as exc:
        raise WorkerError("worker local image input is invalid before SDK start") from exc
    permission_value = capability.get("effective_permission")
    plugin_home = standard_codex_home()
    if isinstance(permission_value, dict):
        try:
            durable_permission = NativePermissionAuthority.from_facts(permission_value)
        except (TypeError, ValueError) as exc:
            raise WorkerError("worker capability has invalid effective native authority") from exc
        native_home = _resolve_shared_native_home(os.environ.get("CODEX_HOME"), fallback_home=Path.home())
        plugin_home = native_home
        try:
            profile = NativeProfileProjection.load(native_home)
            if profile.worker_compatibility_sha256 != capability.get("native_compatibility_sha256"):
                raise ProfileFailure("native compatibility identity changed before SDK identity")
            profile.verify_worker_sources()
        except ProfileFailure:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProfileFailure("shared native Codex profile cannot be revalidated") from exc
        try:
            current_permission = profile.effective_authority(NativePermissionMode.INHERIT_NATIVE)
            effective_permission = durable_permission.meet(current_permission)
        except (ValueError, RuntimeError) as exc:
            raise PermissionFailure("shared native permission authority cannot be reconciled") from exc
        runtime = NativeRuntimeConfig.shared(profile)
        config = CodexSdkConfig(
            str(capsule["model"]),
            ReasoningEffort(str(capsule["reasoning_effort"])),
            sandbox=None,
            cwd=workspace,
            native_runtime=runtime,
            permission_mode=NativePermissionMode(str(effective_permission.mode.value)),
            effective_permission=effective_permission,
            leaf_worker=True,
        )
    else:
        # The low-level donor/test seam may omit native facts.  Keep that seam
        # explicit and bounded; production supervisor capabilities always
        # carry the shared-profile binding above.
        config = CodexSdkConfig(
            str(capsule["model"]),
            ReasoningEffort(str(capsule["reasoning_effort"])),
            sandbox=Sandbox.READ_ONLY,
            cwd=workspace,
            leaf_worker=True,
        )
    adapter = CodexSdkAdapter(config)
    heartbeat_stop = threading.Event()
    heartbeat: threading.Thread | None = None
    control_stop = threading.Event()
    control_thread: threading.Thread | None = None
    live_handle: LiveTurnHandle | None = None
    skill_binding = _verified_skill_input(plugin_requirements, plugin_snapshots, plugin_home)
    skill_binding_entered = False
    sdk_input: str | SkillInput = str(capsule["prompt"])
    # Command application is idempotent within one worker process.  A lost
    # acknowledgement causes the supervisor to return the same ``sent``
    # command on the next poll; never call the SDK handle twice.
    applied_commands: dict[str, tuple[str, str | None]] = {}
    bound_turn_id: str | None = None
    next_event_sequence = 0
    pending_event: dict[str, object] | None = None
    try:
        verified_input = skill_binding.__enter__()
        skill_binding_entered = True
        if verified_input is not None:
            sdk_input = verified_input
        if resume_thread_id is None:
            thread = adapter.start_thread()
        else:
            try:
                thread = adapter.resume_thread(ThreadIdentity(resume_thread_id))
            except ValueError as exc:
                raise WorkerError("durable worker thread identity is invalid") from exc
        # Bind the SDK identity before the turn crosses the external boundary.
        bind_response = send_request(
            socket_path,
            {
                "version": 1,
                "operation": "bind_worker",
                "dispatch_id": capability["dispatch_id"],
                "generation": capability["generation"],
                "attempt": capability["attempt"],
                "token": capability["token"],
                "thread_id": thread.id,
            },
        )
        initial_sequence = bind_response.get("next_event_sequence", 0)
        if isinstance(initial_sequence, bool) or not isinstance(initial_sequence, int) or initial_sequence < 0:
            raise WorkerError("controller returned an invalid diagnostic sequence")
        next_event_sequence = initial_sequence

        def renew() -> None:
            while not heartbeat_stop.wait(5.0):
                try:
                    send_request(
                        socket_path,
                        {
                            "version": 1,
                            "operation": "heartbeat",
                            "dispatch_id": capability["dispatch_id"],
                            "generation": capability["generation"],
                            "attempt": capability["attempt"],
                            "token": capability["token"],
                        },
                        timeout=2.0,
                    )
                except (IpcError, OSError):
                    # The supervisor owns recovery.  A heartbeat failure is
                    # not a model result and must never be turned into one.
                    return

        heartbeat = threading.Thread(target=renew, name="codex-flow-worker-lease", daemon=True)
        heartbeat.start()

        def control_loop(turn_id: str, handle: LiveTurnHandle) -> None:
            """Receive and apply only commands bound to this live turn."""

            while not control_stop.wait(0.1):
                try:
                    response = send_request(
                        socket_path,
                        {
                            "version": 1,
                            "operation": "poll_commands",
                            "dispatch_id": capability["dispatch_id"],
                            "generation": capability["generation"],
                            "attempt": capability["attempt"],
                            "token": capability["token"],
                            "thread_id": thread.id,
                            "turn_id": turn_id,
                        },
                        timeout=2.0,
                    )
                except (IpcError, OSError):
                    # The parent may still be committing the liveness row
                    # immediately after Popen.  Keep the control listener
                    # alive through that narrow startup race.
                    if control_stop.wait(0.25):
                        return
                    continue
                commands = response.get("commands", [])
                if not isinstance(commands, list):
                    return
                history_requests = response.get("history_requests", [])
                if isinstance(history_requests, list):
                    for raw_request in history_requests:
                        if not isinstance(raw_request, dict):
                            continue
                        request_id = raw_request.get("request_id")
                        if not isinstance(request_id, str):
                            continue
                        try:
                            history_request = ConversationHistoryRequest(
                                ConversationSubjectKind(raw_request["subject_kind"]),
                                raw_request["subject_id"],  # type: ignore[arg-type]
                                ThreadIdentity(raw_request["thread_id"]),  # type: ignore[arg-type]
                                raw_request["generation"],  # type: ignore[arg-type]
                                raw_request["attempt"],  # type: ignore[arg-type]
                                raw_request["revision"],  # type: ignore[arg-type]
                                raw_request["page_token"],  # type: ignore[arg-type]
                                raw_request["page_fragments"],  # type: ignore[arg-type]
                            )
                            page = adapter.read_conversation_history(history_request)
                        except (TypeError, ValueError):
                            continue
                        try:
                            send_request(
                                socket_path,
                                {
                                    "version": 1,
                                    "operation": "conversation_history_response",
                                    "request_id": request_id,
                                    "dispatch_id": capability["dispatch_id"],
                                    "generation": capability["generation"],
                                    "attempt": capability["attempt"],
                                    "thread_id": thread.id,
                                    "turn_id": turn_id,
                                    "token": capability["token"],
                                    "page": page.to_json(),
                                },
                                timeout=2.0,
                            )
                        except (IpcError, OSError):
                            return
                for command in commands:
                    if not isinstance(command, dict):
                        continue
                    command_id = command.get("command_id")
                    kind = command.get("kind")
                    if (
                        not isinstance(command_id, str)
                        or command.get("thread_id") != thread.id
                        or command.get("turn_id") != turn_id
                        or command.get("generation") != capability["generation"]
                        or command.get("attempt") != capability["attempt"]
                    ):
                        continue
                    state = "acknowledged"
                    detail: str | None = None
                    prior = applied_commands.get(command_id)
                    if prior is not None:
                        state, detail = prior
                    else:
                        try:
                            if kind == "steer" and isinstance(command.get("payload"), str):
                                handle.steer(str(command["payload"]))
                            elif kind == "interrupt" and command.get("payload") is None:
                                handle.interrupt()
                                # An interrupt acknowledgement is deferred
                                # until the SDK emits terminal ``interrupted``
                                # evidence.  Application of the SDK call is
                                # remembered so replayed ``sent`` rows do not
                                # invoke the handle again.
                                state = "sent"
                            else:
                                raise WorkerError("malformed live control command")
                        except Exception as exc:
                            state = "rejected"
                            detail = redact_diagnostic_text(
                                " ".join(str(exc).split())[:256] or type(exc).__name__, limit=256
                            )
                        applied_commands[command_id] = (state, detail)
                    if kind == "interrupt" and state == "sent":
                        # Terminal evidence in the result path owns the final
                        # acknowledgement.  Keep the command in ``sent``.
                        continue
                    try:
                        send_request(
                            socket_path,
                            {
                                "version": 1,
                                "operation": "ack_control",
                                "dispatch_id": capability["dispatch_id"],
                                "generation": capability["generation"],
                                "attempt": capability["attempt"],
                                "token": capability["token"],
                                "command_id": command_id,
                                "thread_id": thread.id,
                                "turn_id": turn_id,
                                "kind": kind,
                                "state": state,
                                "detail": detail,
                            },
                            timeout=2.0,
                        )
                    except (IpcError, OSError):
                        return

        def bind_turn(turn_id: str) -> None:
            nonlocal bound_turn_id, control_thread, live_handle
            if control_thread is not None:
                control_stop.set()
                control_thread.join(timeout=3.0)
                if control_thread.is_alive():
                    raise WorkerError("prior worker control producer did not quiesce")
                control_stop.clear()
            live_handle = adapter.live_turn(thread, turn_id)
            bound_turn_id = turn_id
            try:
                send_request(
                    socket_path,
                    {
                        "version": 1,
                        "operation": "bind_turn",
                        "dispatch_id": capability["dispatch_id"],
                        "generation": capability["generation"],
                        "attempt": capability["attempt"],
                        "token": capability["token"],
                        "thread_id": thread.id,
                        "turn_id": turn_id,
                    },
                    timeout=2.0,
                )
            except (IpcError, OSError) as exc:
                raise WorkerError("live SDK turn binding failed") from exc
            control_thread = threading.Thread(
                target=control_loop,
                args=(turn_id, live_handle),
                name="codex-flow-live-control",
                daemon=True,
            )
            control_thread.start()

        def deliver_event(candidate: dict[str, object]) -> bool:
            for _attempt in range(3):
                try:
                    response = send_request(socket_path, candidate, timeout=2.0)
                    if response.get("version") == 1 and response.get("ok") is True:
                        return True
                except (IpcError, OSError):
                    continue
            return False

        def emit_event(event: LifecycleEvent) -> None:
            nonlocal next_event_sequence, pending_event
            if _is_lossy_worker_diagnostic(event):
                # High-volume command output deltas are explicitly lossy
                # diagnostics. Completed item/turn events retain authoritative
                # boundaries without allowing output volume to starve result
                # ingress or supervisor lease renewal.
                return
            outbound_turn_id = event.turn_id or bound_turn_id
            if outbound_turn_id is None:
                return
            # LifecycleEvent is a closed typed value; keep the wire payload
            # equally bounded and secretless.  The supervisor redacts again
            # before durable persistence.
            current_sequence = (
                int(pending_event["sequence"]) + 1 if pending_event is not None else next_event_sequence + 1
            )
            current = {
                "version": 1,
                "operation": "worker_event",
                "dispatch_id": capability["dispatch_id"],
                "generation": capability["generation"],
                "attempt": capability["attempt"],
                "token": capability["token"],
                "thread_id": thread.id,
                "turn_id": outbound_turn_id,
                "sequence": current_sequence,
                "kind": event.method,
                "text": event.text,
                "payload_sha256": hashlib.sha256(redact_diagnostic_text(event.text).encode("utf-8")).hexdigest()
                if event.text is not None
                else None,
            }
            if pending_event is not None:
                if not deliver_event(pending_event):
                    return
                next_event_sequence = int(pending_event["sequence"])
                pending_event = None
                current["sequence"] = next_event_sequence + 1
            if not deliver_event(current):
                pending_event = current
                return
            next_event_sequence = int(current["sequence"])

        def flush_pending_event() -> None:
            nonlocal pending_event, next_event_sequence
            if pending_event is not None and deliver_event(pending_event):
                next_event_sequence = int(pending_event["sequence"])
                pending_event = None

        observation = _run_bounded_schema_turn(
            adapter,
            thread,
            sdk_input,
            cast(Schema | None, capsule.get("output_schema")),
            local_image_inputs=local_image_inputs,
            event_callback=emit_event,
            turn_callback=bind_turn,
        )
        if observation.final_response is None:
            raise WorkerError("SDK worker returned no terminal agentMessage")
        flush_pending_event()
        ensure_runtime_dir(result_file.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(result_file, flags, 0o400)
        try:
            _write_once(fd, observation.final_response.encode("utf-8"))
        finally:
            os.close(fd)
        # Terminal result ingress outranks lossy diagnostics and command
        # polling. Quiesce both background socket producers before the exact
        # replay-safe result submission begins.
        control_stop.set()
        if control_thread is not None:
            control_thread.join(timeout=3.0)
            if control_thread.is_alive():
                raise WorkerError("worker control producer did not quiesce before result submission")
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=3.0)
            if heartbeat.is_alive():
                raise WorkerError("worker heartbeat producer did not quiesce before result submission")
        return _submit_result(
            capability_file=capability_file,
            result_file=result_file,
            socket_path=socket_path,
            expected_capability=capability_binding,
        )
    finally:
        control_stop.set()
        if control_thread is not None:
            control_thread.join(timeout=1.0)
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1.0)
        if skill_binding_entered:
            skill_binding.__exit__(None, None, None)
        adapter.close()


def _is_lossy_worker_diagnostic(event: LifecycleEvent) -> bool:
    """Identify high-volume SDK progress events safe to drop.

    These events are useful for a live progress projection but are not
    lifecycle authority.  Keeping them off the synchronous worker IPC path
    prevents a long assistant response or command stream from delaying the
    capability-bound terminal result.  Completed item and turn events are
    deliberately absent from this set.
    """

    return is_lossy_worker_diagnostic_method(event.method)


__all__ = [
    "NESTED_LIFECYCLE_OPERATIONS",
    "RECOVERY_CONTINUATION_PREAMBLE",
    "WORKER_EXIT_AUTHENTICATION",
    "WORKER_EXIT_CAPABILITY",
    "WORKER_EXIT_FAIL_CLOSED",
    "WORKER_EXIT_INTEGRITY",
    "WORKER_EXIT_MALFORMED",
    "WORKER_EXIT_PERMISSION",
    "WORKER_EXIT_PROFILE",
    "WORKER_EXIT_RESPONSE_CHAIN_INVALID",
    "WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY",
    "WORKER_EXIT_SCHEMA_OUTPUT_INVALID",
    "WORKER_EXIT_TERMINAL_AFTER_IDENTITY",
    "WORKER_EXIT_TRANSIENT_AFTER_IDENTITY",
    "WORKER_EXIT_TRANSPORT_BEFORE_IDENTITY",
    "WORKER_EXIT_UNKNOWN_AFTER_IDENTITY",
    "WORKER_EXIT_UNKNOWN_BEFORE_IDENTITY",
    "ResultTransportAfterIdentity",
    "WorkerError",
    "enforce_leaf_operation",
    "recovery_continuation_prompt",
    "run_nested_delegation_sentinel",
    "run_sdk_worker",
    "submit_result",
    "write_capability",
]


def main() -> int:
    parser = argparse.ArgumentParser(prog="codex-flow-worker")
    parser.add_argument("--capability-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--socket-path", required=True, type=Path)
    parser.add_argument("--capsule-file", type=Path)
    parser.add_argument("--resume-thread-id")
    args = parser.parse_args()
    try:
        if args.capsule_file is None:
            submit_result(
                capability_file=args.capability_file, result_file=args.result_file, socket_path=args.socket_path
            )
        else:
            run_sdk_worker(
                capability_file=args.capability_file,
                capsule_file=args.capsule_file,
                result_file=args.result_file,
                socket_path=args.socket_path,
                resume_thread_id=args.resume_thread_id,
            )
    except ResponseChainInvalid as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker response-chain-invalid: {detail}", file=sys.stderr)
        return WORKER_EXIT_RESPONSE_CHAIN_INVALID
    except SchemaOutputInvalid as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker schema-output-invalid: {detail}", file=sys.stderr)
        return WORKER_EXIT_SCHEMA_OUTPUT_INVALID
    except TransientFailureAfterIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker transient-after-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_TRANSIENT_AFTER_IDENTITY
    except ResultTransportAfterIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker result-transport-after-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY
    except AuthenticationFailure as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker authentication-failure: {detail}", file=sys.stderr)
        return WORKER_EXIT_AUTHENTICATION
    except PermissionFailure as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker permission-failure: {detail}", file=sys.stderr)
        return WORKER_EXIT_PERMISSION
    except UnsupportedCapability as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker capability-failure: {detail}", file=sys.stderr)
        return WORKER_EXIT_CAPABILITY
    except ProfileFailure as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker profile-failure: {detail}", file=sys.stderr)
        return WORKER_EXIT_PROFILE
    except IntegrityFailure as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker integrity-failure: {detail}", file=sys.stderr)
        return WORKER_EXIT_INTEGRITY
    except MalformedInputFailure as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker malformed-input: {detail}", file=sys.stderr)
        return WORKER_EXIT_MALFORMED
    except UnknownSdkFailureAfterIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker unknown-sdk-failure-after-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_UNKNOWN_AFTER_IDENTITY
    except UnknownSdkFailureBeforeIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker unknown-sdk-failure-before-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_UNKNOWN_BEFORE_IDENTITY
    except TerminalFailureAfterIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker terminal-after-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_TERMINAL_AFTER_IDENTITY
    except TransportFailureBeforeIdentity as exc:
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        print(f"codex-flow worker transport-before-identity: {detail}", file=sys.stderr)
        return WORKER_EXIT_TRANSPORT_BEFORE_IDENTITY
    except (WorkerError, OSError, ValueError) as exc:
        # Detached workers have no interactive caller. Emit one bounded,
        # sanitized terminal diagnostic so the supervisor service journal can
        # explain a recoverable exit instead of silently cycling attempts.
        detail = redact_diagnostic_text(" ".join(str(exc).split())[:384], limit=384) or "no detail"
        cause = exc.__cause__
        if cause is not None:
            cause_detail = redact_diagnostic_text(" ".join(str(cause).split())[:256], limit=256) or "no detail"
            detail = f"{detail}; cause={type(cause).__name__}: {cause_detail}"
        print(f"codex-flow worker error: {type(exc).__name__}: {detail}", file=sys.stderr)
        return WORKER_EXIT_FAIL_CLOSED
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by detached subprocesses
    raise SystemExit(main())
