"""Detached, event-driven repository harness for H6-E."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backends.codex_sdk import (
    LEAF_WORKER_CONFIG_OVERRIDES,
    CodexSdkAdapter,
    CodexSdkConfig,
    CodexSdkNotifier,
    ThreadInspection,
    ThreadInspectionKind,
    WakeDeliveryAmbiguous,
    WakeDeliveryUnavailable,
)
from .config import AuthorityUnavailable, WorkflowConfigError, load_workflow_config
from .contracts import (
    ModelFacingControllerActionBundle,
    ModelFacingProgramControllerAction,
    ModelFacingProgramControllerActionBundle,
    ModelFacingResult,
    PluginCapabilitySnapshot,
    PluginRequirement,
    review_result_from_agent_message,
)
from .domain import (
    CONTROL_LIST_RESPONSE_MAX_BYTES,
    CONVERSATION_MAX_CONCURRENT_READS,
    CONVERSATION_READ_DEADLINE_SECONDS,
    LIVE_MAX_SUBSCRIBERS,
    LIVE_SUBSCRIBER_QUEUE_MAX_ENTRIES,
    AcceptanceMode,
    BlockerKind,
    BlockerScope,
    CandidateRecord,
    CompatibilityRebind,
    ControlCommandKind,
    ControlCommandState,
    ControllerClaimantKind,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerGenerationState,
    ControllerRecoveryInspectionClaim,
    ControlListKind,
    ControlListPage,
    ControlListPageStatus,
    ControlListRequest,
    ControlListVisibility,
    ConversationHistoryPage,
    ConversationHistoryRequest,
    ConversationHistoryStatus,
    ConversationSubjectKind,
    DispatchId,
    ExecutionCapsule,
    ExecutionStatus,
    LifecyclePhase,
    LiveTurnKeyframe,
    NativePermissionMode,
    ProgramControllerActionKind,
    ProgramControllerDecisionStatus,
    ProgramGraph,
    ReasonCode,
    ReasoningEffort,
    RecoveryActionKind,
    RetryBudgetChange,
    Sandbox,
    ThreadIdentity,
    TypedBlocker,
    WorkerResultRejectionCode,
    WorkflowState,
    conversation_history_page_from_json,
    is_lossy_worker_diagnostic_method,
    redact_diagnostic_text,
    strict_json_loads,
    thaw_json,
)
from .ipc import IpcError, IpcReasonCode, decode_frame, encode_frame, ensure_runtime_dir, peer_uid
from .ledger import HarnessRefreshBlocked, Ledger, LedgerError, RecordNotFound, StaleWriter
from .native_profile import NativeProfileProjection
from .plugin_capabilities import PluginCapabilityError, _verified_skill_input
from .worker import (
    WORKER_EXIT_AUTHENTICATION,
    WORKER_EXIT_CAPABILITY,
    WORKER_EXIT_FAIL_CLOSED,
    WORKER_EXIT_INTEGRITY,
    WORKER_EXIT_MALFORMED,
    WORKER_EXIT_PERMISSION,
    WORKER_EXIT_PROFILE,
    WORKER_EXIT_RESPONSE_CHAIN_INVALID,
    WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY,
    WORKER_EXIT_SCHEMA_OUTPUT_INVALID,
    WORKER_EXIT_TERMINAL_AFTER_IDENTITY,
    WORKER_EXIT_TRANSIENT_AFTER_IDENTITY,
    WORKER_EXIT_TRANSPORT_BEFORE_IDENTITY,
    WORKER_EXIT_UNKNOWN_AFTER_IDENTITY,
    WORKER_EXIT_UNKNOWN_BEFORE_IDENTITY,
    WorkerError,
    _read_private_capability,
    _read_private_file,
    _write_once,
    recovery_continuation_prompt,
    write_capability,
)
from .worktrees import CandidateIntegrityError, WorktreeError, WorktreeManager


class HarnessError(RuntimeError):
    """WorkflowHarness startup, lease, or lifecycle failure."""


class FailedSpawnTerminalizationError(HarnessError):
    """A failed worker launch still requires durable terminalization."""


class WorkerCompatibilityDrift(HarnessError):
    """The verified installed runtime identity tuple differs from authority."""

    def __init__(
        self,
        *,
        queued_profile_sha256: str,
        current_profile_sha256: str,
        queued_compatibility_sha256: str,
        current_compatibility_sha256: str,
    ) -> None:
        self.queued_profile_sha256 = queued_profile_sha256
        self.current_profile_sha256 = current_profile_sha256
        self.queued_compatibility_sha256 = queued_compatibility_sha256
        self.current_compatibility_sha256 = current_compatibility_sha256
        super().__init__(
            "native profile/configuration identity drift before worker launch: "
            f"profile queued={queued_profile_sha256} current={current_profile_sha256}; "
            f"compatibility queued={queued_compatibility_sha256} current={current_compatibility_sha256}"
        )


def _merged_recovery_deadline(
    persisted_deadline: object,
    provider_reset_deadline: str | None,
    *,
    fallback: str,
) -> str:
    """Keep the durable backoff and honor a later typed provider reset."""

    candidates = [fallback]
    for value in (persisted_deadline, provider_reset_deadline):
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError("recovery deadline is malformed")
        normalized = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("recovery deadline is malformed") from exc
        if parsed.tzinfo is None:
            raise ValueError("recovery deadline must be timezone-aware")
        candidates.append(parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"))
    return max(candidates)


class WorkerResultRejected(IpcError):
    """A worker result was rejected with a bounded, sanitized reason code."""

    def __init__(self, code: WorkerResultRejectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class _LiveSubscriber:
    """One bounded, identity-bound in-memory live-frame subscriber."""

    def __init__(self, dispatch_id: str, generation: int, attempt: int, thread_id: str, turn_id: str) -> None:
        self.subscription_id = uuid.uuid4().hex
        self.dispatch_id = dispatch_id
        self.generation = generation
        self.attempt = attempt
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.frames: queue.Queue[LiveTurnKeyframe] = queue.Queue(maxsize=LIVE_SUBSCRIBER_QUEUE_MAX_ENTRIES)
        self.wake = threading.Event()
        self.terminal = False


IPC_ACCEPTED_FRAME_TIMEOUT_SECONDS = 2.0
WORKER_CANCELLATION_STOP_TIMEOUT_SECONDS = 2.0
STATUS_RECENT_ACTIVITY_LIMIT = 4
STATUS_ACTIVITY_TEXT_MAX_BYTES = 512
QUEUE_TRIGGERING_OPERATIONS = frozenset(
    {
        "wake",
        "submit_result",
        "retry",
        "recovery_action",
        "controller_submit_actions",
        "controller_submit_recovered_actions",
        "program_submit_actions",
        "program_submit_recovered_actions",
        "program_start",
    }
)


WorkerCommand = Callable[[dict[str, object], Path, Path, Path], subprocess.Popen[bytes]]
WakeDelivery = Callable[[str, str], str]
ThreadInspector = Callable[[str], ThreadInspection]

_PUBLIC_QUEUE_FIELDS = frozenset(
    {
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
)


def _public_activity(row: dict[str, object]) -> dict[str, object]:
    """Project one durable diagnostic row into the closed client shape."""

    return {
        "sequence": row["sequence"],
        "kind": row["kind"],
        "occurred_at": row["occurred_at"],
        "text": row["text"],
        "payload_sha256": row["payload_sha256"],
    }


def _public_status_activity(row: dict[str, object]) -> dict[str, object]:
    """Project a compact diagnostic summary for the non-diagnostic status API."""

    activity = _public_activity(row)
    text = activity["text"]
    if isinstance(text, str):
        activity["text"] = redact_diagnostic_text(text, limit=STATUS_ACTIVITY_TEXT_MAX_BYTES)
    return activity


def _public_retry_policy(row: dict[str, object]) -> dict[str, object]:
    """Project retry-policy facts without leaking ledger timestamps/shape."""

    return {
        key: row[key]
        for key in (
            "revision",
            "policy_version",
            "pre_identity_budget",
            "invalid_chain_budget",
            "schema_envelope_budget",
            "post_identity_loss_budget",
            "provider_transient_budget",
            "pre_identity_used",
            "invalid_chain_used",
            "schema_envelope_used",
            "post_identity_loss_used",
            "provider_transient_used",
            "provider_transient_grant_used",
            "last_failure",
            "strategy",
            "next_eligible_at",
            "prior_thread_id",
            "prior_turn_id",
            "human_attention_reason",
        )
    }


def _public_control_command(row: dict[str, object]) -> dict[str, object]:
    """Project the existing canonical command row into its closed wire shape."""

    return {
        key: row[key]
        for key in (
            "command_id",
            "dispatch_id",
            "submission_sequence",
            "generation",
            "attempt",
            "thread_id",
            "turn_id",
            "kind",
            "payload",
            "payload_sha256",
            "state",
            "acknowledgement_json",
            "created_at",
            "sent_at",
            "acknowledged_at",
        )
    }


def process_birth_identity(pid: int | None = None) -> str:
    """Return Linux process start-time identity, or a conservative fallback."""

    target = os.getpid() if pid is None else pid
    try:
        fields = Path(f"/proc/{target}/stat").read_text(encoding="ascii").split()
        return fields[21]
    except (OSError, IndexError, ValueError):
        return f"pid:{target}"


class WorkflowHarness:
    """One repository-bound queue owner and local IPC endpoint."""

    def __init__(
        self,
        state_root: Path,
        *,
        runtime_root: Path | None = None,
        lease_seconds: float = 30.0,
        worker_command: Sequence[str] | None = None,
        controller_command: Sequence[str] | None = None,
        program_controller_command: Sequence[str] | None = None,
        wake_delivery: WakeDelivery | None = None,
        thread_inspector: ThreadInspector | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve(strict=True)
        self.state_dir = self.state_root / ".codex-flow"
        self.runtime_root = ensure_runtime_dir(runtime_root or self.state_dir / "runtime")
        self.socket_path = self.runtime_root / "harness.sock"
        self.lease_seconds = lease_seconds
        self.worker_command = tuple(worker_command or (sys.executable, "-m", "codex_flow.worker"))
        self.controller_command = tuple(
            controller_command or (sys.executable, "-m", "codex_flow.cli", "controller-generation")
        )
        self.program_controller_command = tuple(
            program_controller_command or (sys.executable, "-m", "codex_flow.cli", "program-controller-generation")
        )
        self._worktrees = WorktreeManager(mutation_lock_path=self.state_dir / "controller.lock")
        self._wake_delivery = wake_delivery
        self._thread_inspector = thread_inspector
        self.ledger = Ledger(self.state_dir / "workflow.db")
        self.owner_nonce = os.urandom(32).hex()
        self.owner_nonce_sha256 = hashlib.sha256(self.owner_nonce.encode("ascii")).hexdigest()
        self.epoch: int | None = None
        self._socket: socket.socket | None = None
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._controller_children: dict[str, subprocess.Popen[bytes]] = {}
        self._resumed_children: set[str] = set()
        self._active_turns: dict[str, tuple[int, int, str, str]] = {}
        self._stop = False
        self._next_checkpoint_deadline: datetime | None = None
        self._next_renewal_monotonic: float | None = None
        self._conversation_requests: dict[str, dict[str, object]] = {}
        self._conversation_requests_lock = threading.Lock()
        self._conversation_read_slots = threading.BoundedSemaphore(CONVERSATION_MAX_CONCURRENT_READS)
        self._live_subscribers: dict[str, _LiveSubscriber] = {}
        self._live_subscribers_lock = threading.Lock()
        self._live_revisions: dict[tuple[str, int, int, str, str], int] = {}

    def _refresh_checkpoint_deadline(self) -> None:
        value = self.ledger.next_checkpoint_deadline()
        if value is None:
            self._next_checkpoint_deadline = None
            return
        try:
            self._next_checkpoint_deadline = datetime.fromisoformat(value.removesuffix("Z")).replace(
                tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise HarnessError("checkpoint deadline is not a valid UTC timestamp") from exc

    def _select_timeout(self, fallback: float) -> float:
        deadlines = [fallback]
        if self._next_checkpoint_deadline is not None:
            deadlines.append(max(0.0, (self._next_checkpoint_deadline - datetime.now(timezone.utc)).total_seconds()))
        if self._next_renewal_monotonic is not None:
            deadlines.append(max(0.0, self._next_renewal_monotonic - time.monotonic()))
        return min(deadlines)

    def close(self) -> None:
        self._close_live_subject()
        for child in (*self._children.values(), *self._controller_children.values()):
            if child.poll() is None:
                try:
                    child.terminate()
                    child.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        child.kill()
                    except OSError:
                        pass
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        try:
            if os.path.lexists(self.socket_path) and not self.socket_path.is_symlink():
                self.socket_path.unlink()
        except OSError:
            pass
        self.ledger.close()

    def _reap_controller_generations(self) -> bool:
        """Reap controller processes without treating exit as completion."""

        changed = False
        for decision_id, child in tuple(self._controller_children.items()):
            return_code = child.poll()
            if return_code is None:
                continue
            # Program-controller children have no queue dispatch row.  A
            # process that exits while its generation is still
            # ``delivery_starting`` has not crossed the SDK identity
            # boundary; close that exact lineage through the existing
            # inspection CAS before removing it from the in-memory registry.
            # Otherwise the scheduler would treat the orphan as recoverable
            # and launch it again on every steady-state cycle.
            try:
                program_status = self.ledger.program_controller_decision(decision_id)
            except RecordNotFound:
                program_status = None
            if program_status is not None:
                try:
                    generation = self.ledger.controller_generation(
                        program_status.decision_id, int(program_status.current_generation)
                    )
                except RecordNotFound:
                    generation = None
                if generation is not None and (
                    generation.state in {ControllerGenerationState.DELIVERY_STARTING, ControllerGenerationState.ACTIVE}
                    and generation.controller_thread_id is None
                    and generation.controller_turn_id is None
                    and generation.inspection_outcome is None
                ):
                    try:
                        self.ledger._mark_controller_generation_launch_ambiguous(
                            program_status.decision_id,
                            generation=program_status.current_generation,
                            expected_revision=program_status.revision,
                            expected_state=program_status.state,
                            expected_claimant_kind=program_status.claimant_kind,
                            expected_claimant_id=program_status.claimant_id,
                        )
                    except StaleWriter:
                        # Another owner may have completed the exact
                        # pre-identity transition between the two reads.
                        # The durable launch CAS remains authoritative and the
                        # child can be reaped idempotently.
                        pass
                    except (LedgerError, ValueError) as exc:
                        # Do not silently discard a generation whose durable
                        # closure lost a CAS race or failed validation.  The
                        # harness must remain fail-closed until an owner
                        # can reconcile the exact persisted facts.
                        raise HarnessError("program controller pre-identity failure could not be terminalized") from exc
                self._controller_children.pop(decision_id, None)
                changed = True
                continue
            if return_code == WORKER_EXIT_PROFILE:
                try:
                    status = self.ledger.controller_decision(decision_id)
                except RecordNotFound:
                    try:
                        status = self.ledger.program_controller_decision(decision_id)
                        self.ledger.record_program_attention(
                            status.program_id,
                            event_key=f"controller/{decision_id}/profile-drift",
                            payload={
                                "decision_id": decision_id,
                                "generation": int(status.current_generation),
                                "reason": "controller native profile drift",
                            },
                        )
                    except LedgerError as exc:
                        raise HarnessError("program controller profile-drift terminalization failed") from exc
                else:
                    try:
                        self.ledger.record_controller_profile_drift(
                            decision_id,
                            generation=int(status.current_generation),
                        )
                    except LedgerError as exc:
                        # The child crossed a known terminal boundary, but the
                        # corresponding durable closure did not.  Keep lifecycle
                        # authority fail-closed instead of silently allowing the
                        # scheduler to retry the same incompatible profile.
                        raise HarnessError("controller profile-drift terminalization failed") from exc
            self._controller_children.pop(decision_id, None)
            changed = True
        return changed

    @staticmethod
    def _controller_model_and_effort(_row: dict[str, object]) -> tuple[str, str]:
        """Return the repository-owned controller route, never the worker route."""

        return "gpt-6-astra", ReasoningEffort.MEDIUM.value

    def _spawn_controller_generation(self, status: object, *, recovery: bool = False) -> bool:
        """Start one private controller-generation service process.

        The durable generation row is reserved before ``Popen``.  A restart
        therefore sees ``delivery_starting``/``active`` and performs a single
        inspect-before-replace recovery instead of opening a speculative
        second writer.
        """

        from .domain import ControllerDecisionStatus, ControllerGenerationState

        if self.epoch is None or not isinstance(status, ControllerDecisionStatus):
            return False
        decision_id = str(status.decision_id)
        if decision_id in self._controller_children:
            return False
        generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
        if recovery:
            if generation.state not in {
                ControllerGenerationState.PREPARED,
                ControllerGenerationState.DELIVERY_STARTING,
                ControllerGenerationState.ACTIVE,
                ControllerGenerationState.COMPLETED,
                ControllerGenerationState.FAILED,
                ControllerGenerationState.INTERRUPTED,
            }:
                return False
            row = self.ledger.queue_dispatch(status.dispatch_id)
            model, effort = self._controller_model_and_effort(row)
            permission_args = self._controller_permission_args(row)
            command = (
                *self.controller_command,
                "--recover",
                "--decision-id",
                decision_id,
                "--state-root",
                str(self.state_root),
                "--cwd",
                str(row["workspace_path"]),
                "--model",
                model,
                "--reasoning-effort",
                effort,
                *permission_args,
            )
        else:
            if generation.state is not ControllerGenerationState.PREPARED:
                return False
            self.ledger.prepare_controller_generation(
                status.decision_id, generation=status.current_generation, prompt_sha256=None
            )
            row = self.ledger.queue_dispatch(status.dispatch_id)
            model, effort = self._controller_model_and_effort(row)
            permission_args = self._controller_permission_args(row)
            command = (
                *self.controller_command,
                "--decision-id",
                decision_id,
                "--state-root",
                str(self.state_root),
                "--cwd",
                str(row["workspace_path"]),
                "--model",
                model,
                "--reasoning-effort",
                effort,
                *permission_args,
            )
        try:
            launch_authority = self.ledger._controller_generation_launch_authority(
                status.decision_id,
                generation=status.current_generation,
            )
        except LedgerError:
            return False
        try:
            child = subprocess.Popen(command, start_new_session=True, close_fds=True)
        except OSError:
            # Popen is a synchronous pre-identity boundary.  Reset only the
            # exact prepared revision; if a human/model claim won the race,
            # the CAS rejects this reset and leaves the newer claim intact.
            if not recovery:
                try:
                    self.ledger.reset_controller_generation_delivery(
                        status.decision_id,
                        generation=status.current_generation,
                        expected_revision=status.revision,
                        claimant_id="",
                        token="",
                    )
                except LedgerError:
                    pass
            elif (
                generation.inspection_outcome is None
                and status.claimant_kind in {None, ControllerClaimantKind.MODEL}
                and generation.state in {ControllerGenerationState.DELIVERY_STARTING, ControllerGenerationState.ACTIVE}
            ):
                # A restart can leave a delivery_starting/active generation
                # orphaned before this recovery process acquires identity.
                # The failed Popen is an ambiguous launch boundary.  Record
                # one durable human-attention fact through an exact revision
                # and claimant CAS; a concurrent/newer human claim therefore
                # remains untouched and the failed launch cannot be retried as
                # a speculative second writer.
                try:
                    self.ledger._mark_controller_generation_launch_ambiguous(
                        status.decision_id,
                        generation=status.current_generation,
                        expected_revision=status.revision,
                        expected_state=status.state,
                        expected_claimant_kind=status.claimant_kind,
                        expected_claimant_id=status.claimant_id,
                    )
                except LedgerError:
                    pass
            return False
        try:
            self.ledger._bind_controller_generation_launch(
                status.decision_id,
                generation=status.current_generation,
                expected_authority_sha256=launch_authority,
            )
        except LedgerError:
            self._stop_unowned_controller_child(child)
            return False
        self._controller_children[decision_id] = child
        return True

    def _program_controller_permission_args(self, status: ProgramControllerDecisionStatus) -> tuple[str, ...]:
        """Forward one program's first node native authority to its controller."""

        try:
            graph = self.ledger.program_graph(status.program_id)
            capsule = graph.nodes[0].capsule
            home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve(strict=True)
            profile = NativeProfileProjection.load(home)
            profile.verify_sources()
            effective = profile.effective_authority(capsule.permission_mode)
        except (IndexError, OSError, RuntimeError, ValueError):
            # Provider-free ledger fixtures intentionally omit native profile
            # facts.  The private runner remains read-only in that seam; a
            # production profile failure is raised by its own typed boundary.
            return ()
        encoded = json.dumps(effective.facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return (
            "--effective-permission-json",
            encoded,
            "--native-compatibility-sha256",
            profile.worker_compatibility_sha256,
        )

    def _spawn_program_controller_generation(
        self, status: ProgramControllerDecisionStatus, *, recovery: bool = False
    ) -> bool:
        """Launch one ephemeral program-controller generation without a queue row."""

        if self.epoch is None:
            return False
        decision_id = str(status.decision_id)
        if decision_id in self._controller_children:
            return False
        generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
        launchable_recovery = {
            ControllerGenerationState.DELIVERY_STARTING,
            ControllerGenerationState.ACTIVE,
            ControllerGenerationState.COMPLETED,
            ControllerGenerationState.FAILED,
            ControllerGenerationState.INTERRUPTED,
        }
        if recovery:
            if generation.state not in launchable_recovery:
                return False
        elif generation.state is not ControllerGenerationState.PREPARED:
            return False
        if not recovery:
            self.ledger.prepare_controller_generation(
                status.decision_id, generation=status.current_generation, prompt_sha256=None
            )
        model, effort = self._controller_model_and_effort({})
        command = (
            *self.program_controller_command,
            *(("--recover",) if recovery else ()),
            "--decision-id",
            decision_id,
            "--state-root",
            str(self.state_root),
            "--cwd",
            str(self.state_root),
            "--model",
            model,
            "--reasoning-effort",
            effort,
            *self._program_controller_permission_args(status),
        )
        try:
            authority = self.ledger._controller_generation_launch_authority(
                status.decision_id, generation=status.current_generation
            )
        except LedgerError:
            return False
        try:
            child = subprocess.Popen(command, start_new_session=True, close_fds=True)
        except OSError:
            if not recovery:
                try:
                    self.ledger.reset_controller_generation_delivery(
                        status.decision_id,
                        generation=status.current_generation,
                        expected_revision=status.revision,
                        claimant_id="",
                        token="",
                    )
                except LedgerError:
                    pass
            return False
        try:
            self.ledger._bind_controller_generation_launch(
                status.decision_id,
                generation=status.current_generation,
                expected_authority_sha256=authority,
            )
        except LedgerError:
            self._stop_unowned_controller_child(child)
            return False
        self._controller_children[decision_id] = child
        return True

    @staticmethod
    def _stop_unowned_controller_child(child: subprocess.Popen[bytes]) -> None:
        """Boundedly reap a process that lost durable launch authority."""

        try:
            child.terminate()
        except OSError:
            # The process may already have exited between the ownership CAS
            # and termination.  It still needs a bounded wait so the parent
            # does not leave a zombie behind.
            pass
        try:
            child.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

    @staticmethod
    def _controller_permission_args(row: dict[str, object]) -> tuple[str, ...]:
        """Forward the durable native authority facts to the private runner."""

        try:
            route = strict_json_loads(str(row["route_json"]), max_bytes=16_384)
        except (TypeError, ValueError):
            return ()
        if not isinstance(route, dict):
            return ()
        effective = route.get("effective_permission")
        compatibility = route.get("native_compatibility_sha256")
        if not isinstance(effective, dict) or not isinstance(compatibility, str):
            # Hermetic low-level queue fixtures intentionally omit profile
            # identities; their private runner keeps its explicit read-only
            # donor configuration rather than manufacturing native authority.
            return ()
        encoded = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ("--effective-permission-json", encoded, "--native-compatibility-sha256", compatibility)

    def _schedule_controller_generations(self) -> bool:
        """Drive due decisions and at most one process per generation."""

        if self.ledger.harness_refresh_fenced():
            return False
        changed = self._reap_controller_generations()
        changed = self._schedule_program_controller_generations() or changed
        for status in self.ledger.controller_decisions():
            if self.ledger.harness_refresh_fenced():
                break
            # Lease expiry is a durable CAS boundary.  Human claims can be
            # released directly; model claims are released only after the
            # generation has an authoritative one-read inspection.  A stale
            # model claim therefore remains untouched while its recovery
            # subprocess performs that inspection.
            try:
                reaped = self.ledger.reap_controller_claim(status.decision_id)
            except LedgerError:
                reaped = None
            if reaped is not None and reaped != status:
                status = reaped
                changed = True
            if status.state is ControllerDecisionState.AWAITING_CLAIM:
                # A prepared generation has never crossed the external
                # identity boundary and may be launched normally.  Any other
                # durable generation state (delivery_starting/active or a
                # completed persisted inspection) belongs to recovery: a
                # restart must inspect or replay that exact lineage rather
                # than silently abandoning it behind an awaiting decision.
                generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
                changed = (
                    self._spawn_controller_generation(
                        status,
                        recovery=generation.state is not ControllerGenerationState.PREPARED,
                    )
                    or changed
                )
                continue
            if status.state is ControllerDecisionState.CLAIMED:
                # Human ownership resolves the decision through the same
                # typed action client, but it never owns or launches the
                # model generation lineage.  In particular, a human claim
                # racing an already delivery-starting/active generation must
                # not cause the scheduler to create a recovery subprocess.
                if status.claimant_kind is ControllerClaimantKind.HUMAN:
                    continue
                generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
                if (
                    generation.state is ControllerGenerationState.ACTIVE
                    and status.claim_expires_at is not None
                    and status.claim_expires_at
                    > datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    and str(status.decision_id) not in self._controller_children
                ):
                    # A controller that persisted an explicit temporary
                    # provider reset keeps its model claim until that bound.
                    # A harness restart must not consume the one-read
                    # recovery path before the deferred owner is eligible.
                    continue
                if (
                    generation.state
                    in {
                        ControllerGenerationState.DELIVERY_STARTING,
                        ControllerGenerationState.ACTIVE,
                        ControllerGenerationState.COMPLETED,
                        ControllerGenerationState.FAILED,
                        ControllerGenerationState.INTERRUPTED,
                    }
                    and (
                        generation.inspection_outcome is None
                        or generation.inspection_outcome == ControllerGenerationState.COMPLETED.value
                    )
                    and str(status.decision_id) not in self._controller_children
                ):
                    changed = self._spawn_controller_generation(status, recovery=True) or changed
            elif status.state is ControllerDecisionState.ACTION_COMMITTED:
                # A process can crash after the atomic effect commit but
                # before acknowledgement.  The recovery runner has a typed
                # outbox acknowledgement path that does not perform another
                # SDK read, so the receipt can be closed exactly once.
                generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
                if str(status.decision_id) not in self._controller_children:
                    changed = self._spawn_controller_generation(status, recovery=True) or changed
            elif (
                status.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
                and status.action_id is not None
                and status.action_sha256 is not None
                and self.ledger.controller_action_pending_acknowledgement(status.decision_id)
            ):
                # ``require_human_attention`` deliberately preserves the
                # attention state while its effect receipt is committed.  A
                # restart still owes the separate acknowledgement fact, and
                # the recovery runner closes it directly from the durable
                # outbox without inspecting or launching an SDK writer.
                if str(status.decision_id) not in self._controller_children:
                    changed = self._spawn_controller_generation(status, recovery=True) or changed
        return changed

    def _program_dispatch_exists(self, dispatch_id: str) -> bool:
        try:
            self.ledger.queue_dispatch(dispatch_id)
        except RecordNotFound:
            return False
        return True

    def _enqueue_program_worker(
        self,
        *,
        program_id: str,
        milestone_id: str,
        role: str,
        generation: int,
        action_context: Mapping[str, object] | None = None,
    ) -> None:
        """Reuse the canonical Controller enqueue boundary for one effect."""

        from .controller import Controller

        graph = self.ledger.program_graph(program_id)
        node = graph.node(milestone_id)
        dispatch_id = str(DispatchId.from_parts(program_id, milestone_id, role, generation))
        if self._program_dispatch_exists(dispatch_id):
            return
        action_json = (
            json.dumps(dict(action_context), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if action_context is not None
            else None
        )
        controller = Controller(self.state_root, worktrees=self._worktrees)
        try:
            controller.enqueue(
                node.capsule,
                role=role,
                generation=generation,
                backend="sdk_headless",
                action_json=action_json,
                plan_path=graph.plan_path,
                plan_revision_sha256=graph.plan_revision_sha256,
            )
        finally:
            controller.close()

    def _apply_program_external_effect(
        self,
        bundle: ModelFacingProgramControllerActionBundle,
        effect_id: str,
        apply: Callable[[], None],
    ) -> bool:
        state = self.ledger.program_action_effect_state(bundle.action_id, effect_id)
        if state in {"applied", "failed"}:
            return False
        try:
            apply()
        except Exception as exc:
            try:
                self.ledger.record_program_action_effect(
                    bundle.action_id,
                    effect_id,
                    state="failed",
                    error_code=type(exc).__name__,
                )
            except LedgerError as record_error:
                raise HarnessError("program effect failure could not be recorded") from record_error
            raise
        self.ledger.record_program_action_effect(bundle.action_id, effect_id, state="applied")
        return True

    def _apply_program_integration_effect(
        self,
        bundle: ModelFacingProgramControllerActionBundle,
        action: object,
        graph: object,
    ) -> None:
        if not isinstance(action, ModelFacingProgramControllerAction):
            raise HarnessError("program integration effect is not typed")
        if not isinstance(graph, ProgramGraph):
            raise HarnessError("program integration graph is not typed")
        if (
            action.milestone_id is None
            or action.candidate_sha is None
            or action.expected_trunk_head is None
            or action.integration_strategy is None
        ):
            raise HarnessError("program integration effect is incomplete")
        pending = next(
            (
                item
                for item in self.ledger.pending_program_integrations(bundle.program_id)
                if item.get("milestone_id") == action.milestone_id and item.get("candidate_sha") == action.candidate_sha
            ),
            None,
        )
        if pending is None:
            status = self.ledger.program_status(bundle.program_id)
            if any(str(item.milestone_id) == action.milestone_id and item.integrated for item in status.nodes):
                # Git may have advanced successfully before the process lost
                # the effect fact.  The durable program projection is the
                # existing success discriminator for this replay.
                return
            raise WorktreeError("program integration is already terminal without a successful receipt")
        node = graph.node(action.milestone_id)
        try:
            if node.capsule.workspace_mode.value == "current_checkout" and (self.state_root / ".git").exists():
                receipt = self._worktrees.verify_serial_candidate(
                    repository_root=self.state_root,
                    candidate_sha=action.candidate_sha,
                    expected_trunk_head=action.expected_trunk_head,
                    strategy=action.integration_strategy,
                    capsule=node.capsule,
                )
            else:
                receipt = self._worktrees.integrate_candidate(
                    repository_root=self.state_root,
                    candidate_workspace=node.workspace_path,
                    candidate_branch=node.capsule.branch,
                    candidate_sha=action.candidate_sha,
                    expected_trunk_head=action.expected_trunk_head,
                    strategy=action.integration_strategy,
                    capsule=node.capsule,
                )
        except WorktreeError as exc:
            self.ledger.complete_program_integration(
                bundle.program_id,
                action.milestone_id,
                candidate_sha=action.candidate_sha,
                receipt={
                    "program_id": str(bundle.program_id),
                    "milestone_id": action.milestone_id,
                    "candidate_sha": action.candidate_sha,
                    "expected_trunk_head": action.expected_trunk_head,
                    "strategy": action.integration_strategy,
                    "before_trunk_head": action.expected_trunk_head,
                    "error": type(exc).__name__,
                },
                state="conflict",
            )
            # The integration outbox is durably terminal, but the external
            # action itself failed.  Preserve that failed-effect fact so the
            # authenticated IPC caller receives one bounded rejection and
            # replay cannot attempt the Git mutation again.
            raise
        else:
            receipt = {
                **receipt,
                "program_id": str(bundle.program_id),
                "milestone_id": action.milestone_id,
                "expected_trunk_head": action.expected_trunk_head,
            }
            self.ledger.complete_program_integration(
                bundle.program_id,
                action.milestone_id,
                candidate_sha=action.candidate_sha,
                receipt=receipt,
            )

    def _apply_program_action_bundle(self, bundle: ModelFacingProgramControllerActionBundle) -> bool:
        """Execute only the typed effects already committed by the ledger."""

        graph = self.ledger.program_graph(bundle.program_id)
        changed = False
        for action in bundle.actions:
            if action.kind is ProgramControllerActionKind.START_READY_MILESTONES:
                for milestone_id in action.milestone_ids:
                    changed = (
                        self._apply_program_external_effect(
                            bundle,
                            f"start:{milestone_id}",
                            lambda milestone_id=milestone_id: self._enqueue_program_worker(
                                program_id=str(bundle.program_id),
                                milestone_id=milestone_id,
                                role="executor",
                                generation=1,
                            ),
                        )
                        or changed
                    )
            elif action.kind is ProgramControllerActionKind.START_REVIEWS:
                if action.milestone_id is None or action.candidate_sha is None:
                    raise HarnessError("program review effect is incomplete")
                milestone_id = action.milestone_id
                candidate_sha = action.candidate_sha
                for role in action.review_roles:
                    generation = self.ledger.next_program_dispatch_generation(bundle.program_id, milestone_id, role)

                    def enqueue_review_worker(
                        role_value: str = role,
                        milestone_value: str = milestone_id,
                        candidate_value: str = candidate_sha,
                        generation_value: int = generation,
                    ) -> bool:
                        return self._enqueue_program_worker(
                            program_id=str(bundle.program_id),
                            milestone_id=milestone_value,
                            role=role_value,
                            generation=generation_value,
                            action_context={
                                "program_id": str(bundle.program_id),
                                "milestone_id": milestone_value,
                                "candidate_sha": candidate_value,
                                "review_role": role_value,
                            },
                        )

                    changed = (
                        self._apply_program_external_effect(
                            bundle,
                            f"review:{milestone_id}:{role}",
                            enqueue_review_worker,
                        )
                        or changed
                    )
            elif action.kind is ProgramControllerActionKind.REQUEST_REPAIR:
                if action.milestone_id is None or action.candidate_sha is None:
                    raise HarnessError("program repair effect is incomplete")
                milestone_id = action.milestone_id
                candidate_sha = action.candidate_sha
                finding_ids = action.finding_ids
                generation = self.ledger.next_program_dispatch_generation(bundle.program_id, milestone_id, "executor")

                def enqueue_repair_worker(
                    milestone_value: str = milestone_id,
                    candidate_value: str = candidate_sha,
                    finding_values: tuple[str, ...] = finding_ids,
                    generation_value: int = generation,
                ) -> bool:
                    return self._enqueue_program_worker(
                        program_id=str(bundle.program_id),
                        milestone_id=milestone_value,
                        role="executor",
                        generation=generation_value,
                        action_context={
                            "program_id": str(bundle.program_id),
                            "milestone_id": milestone_value,
                            "candidate_sha": candidate_value,
                            "finding_ids": list(finding_values),
                            "repair": True,
                        },
                    )

                changed = (
                    self._apply_program_external_effect(
                        bundle,
                        f"repair:{milestone_id}",
                        enqueue_repair_worker,
                    )
                    or changed
                )
            elif action.kind is ProgramControllerActionKind.INTEGRATE_CANDIDATE:
                assert action.milestone_id is not None and action.candidate_sha is not None
                changed = (
                    self._apply_program_external_effect(
                        bundle,
                        f"integrate:{action.milestone_id}:{action.candidate_sha}",
                        lambda action=action: self._apply_program_integration_effect(bundle, action, graph),
                    )
                    or changed
                )
        return changed

    def _apply_program_action_outbox(self, action_id: str) -> bool:
        rows = self.ledger.program_action_outboxes()
        row = next((item for item in rows if item.get("action_id") == action_id), None)
        if row is None:
            raise RecordNotFound(f"program action outbox does not exist: {action_id}")
        raw = row.get("bundle_json")
        if not isinstance(raw, str):
            raise HarnessError("program action outbox bundle is missing")
        return self._apply_program_action_bundle(ModelFacingProgramControllerActionBundle.from_json_bytes(raw))

    def _reconcile_program_action_outboxes(self) -> bool:
        changed = False
        for row in self.ledger.program_action_outboxes():
            if row.get("state") != "acknowledged" and row.get("state") != "committed":
                continue
            try:
                applied = self._apply_program_action_outbox(str(row["action_id"]))
            except (LedgerError, HarnessError, WorktreeError, TypeError, ValueError):
                continue
            changed = applied or changed
            if row.get("state") == "committed":
                try:
                    bundle = ModelFacingProgramControllerActionBundle.from_json_bytes(str(row["bundle_json"]))
                    if bundle.external_effects and all(
                        self.ledger.program_action_effect_state(bundle.action_id, effect_id) != "pending"
                        for effect_id, _milestone_id in bundle.external_effects
                    ):
                        self.ledger.acknowledge_recovered_controller_action(
                            ControllerDecisionId(str(row["decision_id"])),
                            action_id=str(row["action_id"]),
                            bundle_sha256=str(row["bundle_sha256"]),
                            committed_revision=int(row["expected_revision"]) + 1,
                        )
                        changed = True
                except (LedgerError, TypeError, ValueError):
                    continue
        return changed

    def _schedule_program_controller_generations(self) -> bool:
        changed = self._reconcile_program_action_outboxes()
        changed = bool(self.ledger.reconcile_stale_program_controller_decisions()) or changed
        statuses = self.ledger.program_controller_decisions()
        status_by_decision = {str(status.decision_id): status for status in statuses}
        active_programs = {
            str(status_by_decision[decision_id].program_id)
            for decision_id in self._controller_children
            if decision_id in status_by_decision
        }
        for status in statuses:
            if self.ledger.harness_refresh_fenced():
                break
            program_id = str(status.program_id)
            if program_id in active_programs:
                continue
            generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
            if generation.inspection_outcome == ControllerGenerationState.ACTIVE.value:
                # An authoritative active-writer inspection is itself the
                # durable deferral event.  Lease expiry or a steady-state
                # scheduler cycle must not create a second SDK writer; a
                # later program event creates a fresh decision/generation.
                continue
            if status.state is ControllerDecisionState.AWAITING_CLAIM:
                spawned = self._spawn_program_controller_generation(
                    status,
                    recovery=generation.state is not ControllerGenerationState.PREPARED,
                )
                changed = spawned or changed
                if spawned:
                    active_programs.add(program_id)
            elif status.state is ControllerDecisionState.CLAIMED:
                if status.claimant_kind is ControllerClaimantKind.HUMAN:
                    continue
                generation = self.ledger.controller_generation(status.decision_id, int(status.current_generation))
                if (
                    generation.state
                    in {
                        ControllerGenerationState.DELIVERY_STARTING,
                        ControllerGenerationState.ACTIVE,
                        ControllerGenerationState.COMPLETED,
                        ControllerGenerationState.FAILED,
                        ControllerGenerationState.INTERRUPTED,
                    }
                    and str(status.decision_id) not in self._controller_children
                ):
                    spawned = self._spawn_program_controller_generation(status, recovery=True)
                    changed = spawned or changed
                    if spawned:
                        active_programs.add(program_id)
            elif (
                status.state is ControllerDecisionState.ACTION_COMMITTED
                and status.action_id is not None
                and str(status.decision_id) not in self._controller_children
            ):
                spawned = self._spawn_program_controller_generation(status, recovery=True)
                changed = spawned or changed
                if spawned:
                    active_programs.add(program_id)
        return changed

    def _reap_children(self) -> bool:
        """Observe child exits without polling the queue or Codex tasks."""

        changed = False
        for dispatch_id, child in tuple(self._children.items()):
            return_code = child.poll()
            if return_code is None:
                continue
            # Keep the child and its liveness identity owned by this
            # harness until the durable exit/classification transaction
            # commits.  A transient LedgerError must leave the event
            # retryable in the same epoch rather than stranding starting or
            # running work after the in-memory child entry is discarded.
            try:
                row = self.ledger.queue_dispatch(dispatch_id)
                live = self.ledger.worker_liveness(dispatch_id)
                cancellation = next(
                    (
                        action
                        for action in self.ledger.pending_cancellation_actions()
                        if action["dispatch_id"] == dispatch_id
                    ),
                    None,
                )
                if cancellation is not None and live is not None:
                    self.ledger.acknowledge_worker_cancellation_exit(
                        dispatch_id,
                        action_id=str(cancellation["action_id"]),
                        pid=int(live["pid"]),
                        process_birth_identity=str(live["process_birth_identity"]),
                        exit_code=return_code,
                    )
                    row = self.ledger.queue_dispatch(dispatch_id)
                    live = self.ledger.worker_liveness(dispatch_id)
                if live is not None:
                    if live["exited_at"] is None:
                        self.ledger.mark_worker_exit(
                            dispatch_id,
                            pid=int(live["pid"]),
                            process_birth_identity=str(live["process_birth_identity"]),
                            exit_code=return_code,
                            classification=(
                                "transport-before-identity"
                                if return_code == WORKER_EXIT_TRANSPORT_BEFORE_IDENTITY
                                else "response-chain-invalid"
                                if return_code == WORKER_EXIT_RESPONSE_CHAIN_INVALID
                                else "schema-output-invalid"
                                if return_code == WORKER_EXIT_SCHEMA_OUTPUT_INVALID
                                else "transient-after-identity"
                                if return_code == WORKER_EXIT_TRANSIENT_AFTER_IDENTITY
                                else "authentication-failure"
                                if return_code == WORKER_EXIT_AUTHENTICATION
                                else "permission-failure"
                                if return_code == WORKER_EXIT_PERMISSION
                                else "capability-failure"
                                if return_code == WORKER_EXIT_CAPABILITY
                                else "profile-failure"
                                if return_code == WORKER_EXIT_PROFILE
                                else "result-transport-after-identity"
                                if return_code == WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY
                                else "integrity-failure"
                                if return_code == WORKER_EXIT_INTEGRITY
                                else "malformed-input"
                                if return_code in {WORKER_EXIT_MALFORMED, WORKER_EXIT_FAIL_CLOSED}
                                else "unknown-sdk-failure"
                                if return_code
                                in {WORKER_EXIT_UNKNOWN_AFTER_IDENTITY, WORKER_EXIT_UNKNOWN_BEFORE_IDENTITY}
                                else "terminal-after-identity"
                                if return_code == WORKER_EXIT_TERMINAL_AFTER_IDENTITY
                                else "worker-exit"
                            ),
                        )
                elif row["state"] not in {"completed", "failed", "cancelled"}:
                    self.ledger.mark_human_attention_required(
                        dispatch_id,
                        reason="worker exited without a persisted liveness identity",
                    )
                durable = self.ledger.queue_dispatch(dispatch_id)
                if durable["state"] == "human_attention_required":
                    try:
                        self.ledger.rearm_checkpoint(dispatch_id, seconds=0.001)
                    except StaleWriter:
                        # One already-armed wake or active controller owns the
                        # recovery decision.  Child reaping remains idempotent.
                        pass
            except LedgerError:
                continue
            self._children.pop(dispatch_id, None)
            self._resumed_children.discard(dispatch_id)
            active_turns = getattr(self, "_active_turns", None)
            if active_turns is not None:
                active = active_turns.pop(dispatch_id, None)
                if active is not None:
                    self._close_live_subject(dispatch_id, active)
            # A child exit is an event even if the liveness bind raced with a
            # very fast process.  The durable transition above has either
            # closed the active row or proved that it was already terminal.
            changed = True
            # Every exit, including a provider response-chain rejection, now
            # enters the same inspect-before-mutate recovery boundary.  The
            # old automatic fresh retry was unsafe because it could create a
            # writer without proving that the persisted thread was idle.
            # Recovery policy, not an exit code, owns any continuation/fresh
            # thread decision.
        return changed

    @staticmethod
    def _exact_process_is_live(pid: int, birth_identity: str) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return process_birth_identity(pid) == birth_identity

    def _advance_worker_cancellation(self, action: dict[str, object]) -> bool:
        """Stop and acknowledge one exact owned child under a bounded deadline."""

        dispatch_id = str(action["dispatch_id"])
        queue = self.ledger.queue_dispatch(dispatch_id)
        if queue["state"] == "cancelled":
            return True
        live = self.ledger.worker_liveness(dispatch_id)
        if live is None or live["exited_at"] is not None:
            return False
        pid = int(live["pid"])
        birth_identity = str(live["process_birth_identity"])
        child = self._children.get(dispatch_id)
        exit_code: int | None = None
        if child is not None:
            child_pid = getattr(child, "pid", pid)
            if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid != pid:
                raise LedgerError("cancellation child identity conflicts with durable liveness")
            exit_code = child.poll()
            if exit_code is None:
                try:
                    child.terminate()
                    exit_code = child.wait(timeout=WORKER_CANCELLATION_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    child.kill()
                    try:
                        exit_code = child.wait(timeout=WORKER_CANCELLATION_STOP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        return False
                except OSError:
                    return False
        else:
            if self._exact_process_is_live(pid, birth_identity):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    return False
                deadline = time.monotonic() + WORKER_CANCELLATION_STOP_TIMEOUT_SECONDS
                while time.monotonic() < deadline and self._exact_process_is_live(pid, birth_identity):
                    time.sleep(0.02)
                if self._exact_process_is_live(pid, birth_identity):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        return False
                    deadline = time.monotonic() + WORKER_CANCELLATION_STOP_TIMEOUT_SECONDS
                    while time.monotonic() < deadline and self._exact_process_is_live(pid, birth_identity):
                        time.sleep(0.02)
                if self._exact_process_is_live(pid, birth_identity):
                    return False
        self.ledger.acknowledge_worker_cancellation_exit(
            dispatch_id,
            action_id=str(action["action_id"]),
            pid=pid,
            process_birth_identity=birth_identity,
            exit_code=exit_code,
        )
        self._children.pop(dispatch_id, None)
        self._resumed_children.discard(dispatch_id)
        active_turns = getattr(self, "_active_turns", None)
        if active_turns is not None:
            active = active_turns.pop(dispatch_id, None)
            if active is not None:
                self._close_live_subject(dispatch_id, active)
        return True

    def _advance_pending_cancellations(self) -> bool:
        changed = False
        for action in self.ledger.pending_cancellation_actions():
            try:
                changed = self._advance_worker_cancellation(action) or changed
            except LedgerError:
                continue
        return changed

    def acquire(self) -> dict[str, object]:
        legacy_socket = self.runtime_root / "supervisor.sock"
        if os.path.lexists(legacy_socket):
            raise HarnessError("legacy supervisor socket path remains")
        existing = self.ledger.harness_authority()
        if existing is not None and int(existing["pid"]) != os.getpid():
            try:
                os.kill(int(existing["pid"]), 0)
                live = process_birth_identity(int(existing["pid"])) == str(existing["process_birth_identity"])
            except OSError:
                live = False
            if live:
                raise HarnessError("another live harness owns the repository")
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        fact = self.ledger.acquire_harness(
            repository_root=self.state_root,
            state_root=self.state_root,
            pid=os.getpid(),
            process_birth_identity=process_birth_identity(),
            executable_digest=digest,
            version="0.2.0",
            owner_nonce_sha256=self.owner_nonce_sha256,
            lease_seconds=self.lease_seconds,
        )
        self.epoch = int(fact["epoch"])
        if os.path.lexists(self.socket_path):
            if self.socket_path.is_symlink() or not self.socket_path.is_socket():
                raise HarnessError("harness socket path is unsafe")
            self.socket_path.unlink()
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.bind(os.fspath(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        endpoint.listen(16)
        endpoint.setblocking(False)
        self._socket = endpoint
        return fact

    def rebind_legacy_active_queue(
        self,
        dispatch_id: str,
        *,
        source_thread_id: str,
        native_home: Path | None = None,
    ) -> dict[str, object]:
        """Recover missing v10 authority before restarting one active worker."""

        queue = self.ledger.queue_dispatch(dispatch_id)
        try:
            capsule = strict_json_loads(str(queue["capsule_json"]), max_bytes=2_000_000)
        except ValueError as exc:
            raise HarnessError("legacy queue capsule is not valid bounded JSON") from exc
        if not isinstance(capsule, dict):
            raise HarnessError("legacy queue capsule root must be an object")
        try:
            permission_mode = NativePermissionMode(str(capsule["permission_mode"]))
            source = ThreadIdentity(source_thread_id)
        except (KeyError, ValueError) as exc:
            raise HarnessError("legacy queue lacks original permission or source authority") from exc
        home = (native_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).resolve(strict=True)
        try:
            profile = NativeProfileProjection.load(home)
            profile.verify_worker_sources()
            effective = profile.effective_authority(permission_mode)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HarnessError("shared native Codex profile cannot authorize legacy recovery") from exc
        return self.ledger.rebind_legacy_active_queue(
            dispatch_id,
            source_thread_id=source.id,
            permission_mode=permission_mode,
            native_profile_sha256=profile.profile_sha256,
            native_compatibility_sha256=profile.worker_compatibility_sha256,
            effective_permission=effective,
        )

    def adopt_program_candidate(
        self,
        program_id: str,
        milestone_id: str,
        candidate_sha: str,
        *,
        dispatch_id: str | None = None,
    ) -> object:
        """Adopt one exact existing workspace commit without replaying a worker."""

        graph = self.ledger.program_graph(program_id)
        node = graph.node(milestone_id)
        candidate = self._worktrees.adopt_candidate(node.capsule, candidate_sha)
        if candidate.workspace_path != node.capsule.workspace_path:
            raise WorktreeError("adopted candidate workspace does not match the dispatch capsule")
        return self.ledger.adopt_program_candidate(
            program_id,
            milestone_id,
            candidate,
            dispatch_id=dispatch_id,
        )

    def reconcile_control_execution(self, run_id: str, milestone_id: str) -> CandidateRecord:
        """Adopt one already-completed ordinary execution without a provider call."""

        execution = self.ledger.get_execution(run_id, milestone_id)
        if execution.status is not ExecutionStatus.COMPLETED:
            raise HarnessError("ordinary control reconciliation requires a completed execution")
        capsule = self._control_capsule(run_id, milestone_id)
        integrity = self.ledger.get_execution_integrity(run_id, milestone_id)
        retained_sha = integrity.workspace_terminal_head_sha
        if retained_sha is None:
            raise WorktreeError("completed control execution has no durable terminal candidate identity")
        self._assert_control_terminal_authority(
            capsule,
            candidate_sha=retained_sha,
            require_terminal_facts=True,
        )
        candidate = self._worktrees.inspect_terminal_workspace(capsule, candidate_sha=retained_sha)
        if candidate.commit_sha is None or candidate.disposition.value != "verified_commit":
            raise WorktreeError("completed control execution has no verified candidate")
        dispatch_id = DispatchId.from_parts(run_id, milestone_id, "executor", 1)
        self.ledger.record_control_executor_result(
            run_id,
            milestone_id,
            candidate=candidate,
            terminal_status="completed",
            dispatch_id=dispatch_id,
        )
        self._queue_control_reviews(capsule, candidate.commit_sha)
        return candidate

    def _assert_control_terminal_authority(
        self,
        capsule: ExecutionCapsule,
        *,
        candidate_sha: str,
        require_terminal_facts: bool = False,
    ) -> None:
        """Bind a control candidate to its durable terminal workspace facts.

        Detached queue-only executions do not populate the ordinary execution
        integrity row; those rows retain their existing candidate inspection
        path.  Once a controller execution is terminal, however, accepting a
        candidate from a later checkout view would allow a moved HEAD, dirty
        bytes, or changed Git/protected authority to masquerade as the
        retained result.  Compare every terminal fact before recording any
        acceptance lifecycle event.
        """

        try:
            execution = self.ledger.get_execution(capsule.run_id, capsule.milestone_id)
            integrity = self.ledger.get_execution_integrity(capsule.run_id, capsule.milestone_id)
        except (RecordNotFound, KeyError):
            if require_terminal_facts:
                raise WorktreeError("completed control execution lacks durable terminal integrity facts") from None
            return
        if execution.status is not ExecutionStatus.COMPLETED:
            if require_terminal_facts:
                raise WorktreeError("control candidate adoption requires a completed execution")
            return
        terminal_head = integrity.workspace_terminal_head_sha
        terminal_workspace = integrity.workspace_terminal
        terminal_git = integrity.git_authority_after_sha256
        terminal_protected = execution.protected_after_sha256
        if terminal_head is None or terminal_workspace is None or terminal_git is None or terminal_protected is None:
            raise WorktreeError("completed control execution has incomplete terminal integrity facts")
        if candidate_sha != terminal_head:
            raise WorktreeError("control candidate does not match the durable terminal HEAD")

        try:
            from .controller import (
                Controller,
                git_authority_snapshot,
                protected_paths_digest,
            )

            current_head = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=capsule.workspace_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if current_head.returncode != 0:
                raise WorktreeError("unable to inspect control workspace HEAD")
            status = subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=all"),
                cwd=capsule.workspace_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if status.returncode != 0:
                raise WorktreeError("unable to inspect control workspace status")
            if status.stdout.strip():
                raise WorktreeError("control workspace contains dirty bytes after terminalization")
            current_head_value = current_head.stdout.strip()
            if current_head_value != terminal_head:
                raise WorktreeError("control workspace HEAD advanced after terminalization")
            snapshot_controller = Controller(self.state_root, worktrees=self._worktrees)
            try:
                current_workspace = snapshot_controller._owned_workspace_snapshot(capsule)
            finally:
                snapshot_controller.close()
            if current_workspace != terminal_workspace:
                raise WorktreeError("control workspace bytes changed after terminalization")
            if git_authority_snapshot(capsule.workspace_path).sha256 != terminal_git:
                raise WorktreeError("control Git authority changed after terminalization")
            if protected_paths_digest(capsule.workspace_path, capsule.protected_paths) != terminal_protected:
                raise WorktreeError("control protected paths changed after terminalization")
        except WorktreeError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorktreeError("control terminal integrity could not be revalidated") from exc

    def _capture_control_repair_terminal_integrity(self, row: Mapping[str, object]) -> Mapping[str, object] | None:
        """Capture one generation-2 executor checkout at turn completion."""

        if str(row.get("role")) != "executor" or int(row.get("generation", 0)) != 2:
            return None
        try:
            self.ledger.program_graph(str(row.get("run_id")))
        except RecordNotFound:
            pass
        else:
            return None
        run_id = row.get("run_id")
        milestone_id = row.get("milestone_id")
        action_raw = row.get("action_json")
        if not isinstance(run_id, str) or not isinstance(milestone_id, str) or not isinstance(action_raw, str):
            raise WorktreeError("control repair dispatch lacks durable identity")
        try:
            action = strict_json_loads(action_raw, max_bytes=16_384)
        except ValueError:
            raise WorktreeError("control repair dispatch context is malformed") from None
        if not isinstance(action, dict) or set(action) != {"candidate_sha", "finding_ids", "repair_generation"}:
            raise WorktreeError("control repair dispatch context has an unsupported shape")
        predecessor = action.get("candidate_sha")
        findings = action.get("finding_ids")
        if (
            not isinstance(predecessor, str)
            or not isinstance(findings, list)
            or not findings
            or any(not isinstance(item, str) for item in findings)
            or action.get("repair_generation") != 2
        ):
            raise WorktreeError("control repair dispatch context is not bound to its generation")
        capsule = self._control_capsule(run_id, milestone_id)
        try:
            retained = self.ledger.dispatch_terminal_integrity(str(row.get("dispatch_id")))
        except RecordNotFound:
            pass
        else:
            return {
                "workspace_terminal_head_sha": retained["workspace_terminal_head_sha"],
                "workspace_terminal": retained["workspace_terminal"],
                "git_authority_sha256": retained["git_authority_sha256"],
                "protected_paths_sha256": retained["protected_paths_sha256"],
                "captured_at": retained["captured_at"],
            }
        candidate = self._worktrees.inspect_terminal_workspace(capsule, predecessor_sha=predecessor)
        if (
            candidate.disposition.value != "verified_commit"
            or candidate.commit_sha != candidate.workspace_head
            or candidate.dirty
        ):
            raise WorktreeError("control repair did not produce one direct clean terminal successor")
        try:
            from .controller import Controller, git_authority_snapshot, protected_paths_digest

            parent = subprocess.run(
                ("git", "show", "-s", "--format=%P", candidate.commit_sha or ""),
                cwd=capsule.workspace_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if parent.returncode != 0 or parent.stdout.strip() != predecessor:
                raise WorktreeError("control repair did not produce one direct clean terminal successor")
            execution = self.ledger.get_execution(run_id, milestone_id)
            protected = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
            if execution.protected_after_sha256 is None or protected != execution.protected_after_sha256:
                raise WorktreeError("control repair changed protected terminal authority")
            snapshot_controller = Controller(self.state_root, worktrees=self._worktrees)
            try:
                workspace = snapshot_controller._owned_workspace_snapshot(capsule)
            finally:
                snapshot_controller.close()
            return {
                "workspace_terminal_head_sha": candidate.commit_sha,
                "workspace_terminal": workspace,
                "git_authority_sha256": git_authority_snapshot(capsule.workspace_path).sha256,
                "protected_paths_sha256": protected,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            }
        except WorktreeError:
            raise
        except (RecordNotFound, OSError, RuntimeError, ValueError) as exc:
            raise WorktreeError("control repair terminal authority could not be captured") from exc

    def _assert_control_repair_terminal_authority(
        self,
        capsule: ExecutionCapsule,
        *,
        dispatch_id: str,
        candidate_sha: str,
        result_sha256: str | None,
    ) -> None:
        """Revalidate one immutable repair capture before or after binding."""

        try:
            integrity = self.ledger.dispatch_terminal_integrity(dispatch_id)
            queue_row = self.ledger.queue_dispatch(dispatch_id)
        except (LedgerError, KeyError, ValueError) as exc:
            raise WorktreeError("control repair lacks durable dispatch terminal integrity") from exc
        queue_identity_conflicts = (
            queue_row.get("run_id") != str(capsule.run_id)
            or queue_row.get("milestone_id") != str(capsule.milestone_id)
            or queue_row.get("role") != "executor"
            or queue_row.get("generation") != 2
            or integrity["dispatch_id"] != dispatch_id
            or integrity["workspace_terminal_head_sha"] != candidate_sha
        )
        if result_sha256 is None:
            result_identity_conflicts = (
                integrity["result_sha256"] is not None
                or queue_row.get("raw_result_sha256") is not None
                or queue_row.get("terminal_status") is not None
            )
        else:
            result_identity_conflicts = (
                queue_row.get("terminal_status") != "completed"
                or queue_row.get("raw_result_sha256") != result_sha256
                or integrity["result_sha256"] != result_sha256
            )
        if queue_identity_conflicts or result_identity_conflicts:
            raise WorktreeError("control repair dispatch terminal integrity is stale or conflicting")
        try:
            from .controller import Controller, git_authority_snapshot, protected_paths_digest

            head = subprocess.run(
                ("git", "rev-parse", "--verify", "HEAD^{commit}"),
                cwd=capsule.workspace_path,
                text=True,
                capture_output=True,
                check=False,
            )
            status = subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=all"),
                cwd=capsule.workspace_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if head.returncode != 0 or status.returncode != 0:
                raise WorktreeError("control repair workspace could not be inspected")
            if status.stdout.strip():
                raise WorktreeError("control repair workspace contains dirty bytes after terminalization")
            if head.stdout.strip() != candidate_sha:
                raise WorktreeError("control repair workspace HEAD changed after terminalization")
            snapshot_controller = Controller(self.state_root, worktrees=self._worktrees)
            try:
                workspace = snapshot_controller._owned_workspace_snapshot(capsule)
            finally:
                snapshot_controller.close()
            if workspace != integrity["workspace_terminal"]:
                raise WorktreeError("control repair workspace bytes changed after terminalization")
            if git_authority_snapshot(capsule.workspace_path).sha256 != integrity["git_authority_sha256"]:
                raise WorktreeError("control repair Git authority changed after terminalization")
            if (
                protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
                != integrity["protected_paths_sha256"]
            ):
                raise WorktreeError("control repair protected paths changed after terminalization")
        except WorktreeError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorktreeError("control repair terminal integrity could not be revalidated") from exc

    def _assert_control_repair_preingestion_authority(self, row: Mapping[str, object]) -> None:
        """Require the existing completed-event capture without recapturing."""

        if str(row.get("role")) != "executor" or int(row.get("generation", 0)) != 2:
            return
        try:
            self.ledger.program_graph(str(row.get("run_id")))
        except RecordNotFound:
            pass
        else:
            return
        run_id = row.get("run_id")
        milestone_id = row.get("milestone_id")
        dispatch_id = row.get("dispatch_id")
        if not isinstance(run_id, str) or not isinstance(milestone_id, str) or not isinstance(dispatch_id, str):
            raise WorktreeError("control repair dispatch lacks durable identity")
        try:
            integrity = self.ledger.dispatch_terminal_integrity(dispatch_id)
        except (LedgerError, KeyError, ValueError) as exc:
            raise WorktreeError("control repair lacks pre-ingestion terminal integrity") from exc
        candidate_sha = integrity.get("workspace_terminal_head_sha")
        if not isinstance(candidate_sha, str):
            raise WorktreeError("control repair pre-ingestion terminal integrity is malformed")
        self._assert_control_repair_terminal_authority(
            self._control_capsule(run_id, milestone_id),
            dispatch_id=dispatch_id,
            candidate_sha=candidate_sha,
            result_sha256=None,
        )

    @staticmethod
    def _controller_status_payload(status: object) -> dict[str, object]:
        from .domain import ControllerDecisionStatus

        if not isinstance(status, ControllerDecisionStatus):
            raise IpcError("controller decision status is not typed")
        summary = status.summary
        return {
            "decision_id": str(status.decision_id),
            "dispatch_id": str(status.dispatch_id),
            "kind": status.kind,
            "state": status.state.value,
            "revision": status.revision,
            "current_generation": int(status.current_generation),
            "generation_budget": status.generation_budget,
            "generation_used": status.generation_used,
            "claimant_kind": status.claimant_kind.value if status.claimant_kind else None,
            "claimant_id": status.claimant_id,
            "claim_expires_at": status.claim_expires_at,
            "action_id": status.action_id,
            "action_sha256": status.action_sha256,
            "deadline": status.deadline,
            "summary": {
                "dispatch_id": str(summary.dispatch_id),
                "kind": summary.kind,
                "state": summary.state.value,
                "summary": summary.summary,
                "source_thread_id": summary.source_thread_id,
                "deadline": summary.deadline,
                "revision": summary.revision,
                "expected_successor_dispatch_ids": list(summary.expected_successor_dispatch_ids),
                **summary.context_json(),
            },
        }

    def _status_page_item(self, row: Mapping[str, object]) -> dict[str, object]:
        """Project one source-owned worker row without changing its ordering."""

        item = {key: row[key] for key in _PUBLIC_QUEUE_FIELDS if key in row}
        item["retry_policy"] = _public_retry_policy(self.ledger.retry_policy(str(row["dispatch_id"])))
        item["recent_activity"] = [
            _public_status_activity(activity)
            for activity in self.ledger.recent_activity(str(row["dispatch_id"]), limit=STATUS_RECENT_ACTIVITY_LIMIT)
        ]
        active_turns = getattr(self, "_active_turns", {})
        active = active_turns.get(str(row["dispatch_id"]))
        item["active_turn_id"] = active[3] if active is not None else None
        return item

    @staticmethod
    def _page_token_key(value: object) -> tuple[object, ...]:
        if not isinstance(value, list) or len(value) not in {3}:
            raise ValueError("page token ordering key is malformed")
        if isinstance(value[0], bool) or not isinstance(value[0], int):
            raise ValueError("page token ordering key is malformed")
        if isinstance(value[1], bool) or not isinstance(value[1], int | str):
            raise ValueError("page token ordering key is malformed")
        if not isinstance(value[2], str):
            raise ValueError("page token ordering key is malformed")
        return (value[0], value[1], value[2])

    def _encode_control_page_token(
        self,
        *,
        kind: ControlListKind,
        visibility: ControlListVisibility,
        snapshot_id: str,
        last_key: tuple[object, ...],
    ) -> str:
        if self.epoch is None:
            raise IpcError("harness lease is not acquired")
        body = json.dumps(
            {
                "kind": kind.value,
                "visibility": visibility.value,
                "snapshot_id": snapshot_id,
                "schema": 19,
                "epoch": self.epoch,
                "ledger": hashlib.sha256(os.fspath(self.ledger.path.resolve()).encode("utf-8")).hexdigest(),
                "last_key": list(last_key),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signature = hmac.new(self.owner_nonce.encode("ascii"), body, hashlib.sha256).digest()
        # Keep the framing delimiter outside the authenticated byte strings.
        # A raw HMAC digest may itself contain 0x2e, so framing the body and
        # digest before one base64 encoding is ambiguous on decode.
        body_part = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
        signature_part = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        token = f"{body_part}.{signature_part}"
        if len(token) > 1_024:
            raise IpcError("page continuation token is too large", reason_code="malformed_page_token")
        return token

    def _decode_control_page_token(self, token: object) -> dict[str, object]:
        if (
            not isinstance(token, str)
            or not token
            or any(ord(character) > 0x7F for character in token)
            or len(token.encode("ascii")) > 1_024
        ):
            raise IpcError("page continuation token is malformed", reason_code="malformed_page_token")
        try:
            body_part, signature_part = token.split(".", 1)
            if not body_part or not signature_part or "." in signature_part:
                raise ValueError("page token framing is invalid")
            body_encoded = body_part + "=" * (-len(body_part) % 4)
            signature_encoded = signature_part + "=" * (-len(signature_part) % 4)
            body = base64.b64decode(body_encoded.encode("ascii"), altchars=b"-_", validate=True)
            signature = base64.b64decode(signature_encoded.encode("ascii"), altchars=b"-_", validate=True)
            expected = hmac.new(self.owner_nonce.encode("ascii"), body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("page token signature is invalid")
            decoded = strict_json_loads(body, max_bytes=4_096)
        except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
            raise IpcError("page continuation token is malformed", reason_code="malformed_page_token") from exc
        if not isinstance(decoded, dict) or set(decoded) != {
            "kind",
            "visibility",
            "snapshot_id",
            "schema",
            "epoch",
            "ledger",
            "last_key",
        }:
            raise IpcError("page continuation token is malformed", reason_code="malformed_page_token")
        try:
            ControlListKind(decoded["kind"])
            ControlListVisibility(decoded["visibility"])
            if (
                not isinstance(decoded["snapshot_id"], str)
                or re.fullmatch(r"[0-9a-f]{64}", decoded["snapshot_id"]) is None
            ):
                raise ValueError("page token snapshot is malformed")
            if decoded["schema"] != 19 or isinstance(decoded["epoch"], bool) or not isinstance(decoded["epoch"], int):
                raise ValueError("page token schema is malformed")
            if not isinstance(decoded["ledger"], str) or re.fullmatch(r"[0-9a-f]{64}", decoded["ledger"]) is None:
                raise ValueError("page token ledger is malformed")
            decoded["last_key"] = list(self._page_token_key(decoded["last_key"]))
        except (TypeError, ValueError, KeyError) as exc:
            raise IpcError("page continuation token is malformed", reason_code="malformed_page_token") from exc
        return decoded

    def _control_list_page(self, request: ControlListRequest) -> dict[str, object]:
        if self.epoch is None:
            raise IpcError("harness lease is not acquired")
        snapshot_id = self.ledger.control_list_fingerprint(
            request.list_kind, request.visibility, harness_epoch=self.epoch
        )
        after_key: tuple[object, ...] | None = None
        if request.page_token is not None:
            token = self._decode_control_page_token(request.page_token)
            token_identity = {
                "kind": request.list_kind.value,
                "visibility": request.visibility.value,
                "schema": 19,
                "epoch": self.epoch,
                "ledger": hashlib.sha256(os.fspath(self.ledger.path.resolve()).encode("utf-8")).hexdigest(),
            }
            if any(token.get(key) != value for key, value in token_identity.items()):
                raise IpcError("page continuation identity does not match", reason_code="page_token_identity_mismatch")
            if token["snapshot_id"] != snapshot_id:
                page = ControlListPage(
                    request.list_kind,
                    request.visibility,
                    snapshot_id,
                    (),
                    None,
                    False,
                    ControlListPageStatus.STALE,
                )
                return {"version": 2, "ok": True, "page": page.to_json()}
            after_key = tuple(token["last_key"])  # type: ignore[arg-type]

        entries = self.ledger.control_list_rows(request.list_kind, request.visibility, after_key=after_key)
        selected: list[dict[str, object]] = []
        selected_keys: list[tuple[object, ...]] = []
        for key, row in entries:
            item = (
                self._status_page_item(row)
                if request.list_kind is ControlListKind.WORKERS
                else self._controller_status_payload(self.ledger.controller_decision(str(row["decision_id"])))
            )
            candidate_items = [*selected, item]
            candidate_key = key
            candidate_next_token = (
                self._encode_control_page_token(
                    kind=request.list_kind,
                    visibility=request.visibility,
                    snapshot_id=snapshot_id,
                    last_key=candidate_key,
                )
                if len(candidate_items) < len(entries)
                else None
            )
            page = ControlListPage(
                request.list_kind,
                request.visibility,
                snapshot_id,
                tuple(candidate_items),
                candidate_next_token,
                candidate_next_token is None,
            )
            response = {"version": 2, "ok": True, "page": page.to_json()}
            if len(encode_frame(response)) - 4 > CONTROL_LIST_RESPONSE_MAX_BYTES:
                if not selected:
                    raise IpcError("control list item exceeds page budget", reason_code="item_too_large")
                break
            selected.append(item)
            selected_keys.append(candidate_key)
            if len(selected) >= request.page_items:
                break
        complete = len(selected) >= len(entries)
        if selected and len(selected) < len(entries):
            complete = False
        next_token = None
        if selected and not complete:
            next_token = self._encode_control_page_token(
                kind=request.list_kind,
                visibility=request.visibility,
                snapshot_id=snapshot_id,
                last_key=selected_keys[-1],
            )
        page = ControlListPage(
            request.list_kind,
            request.visibility,
            snapshot_id,
            tuple(selected),
            next_token,
            complete,
        )
        response = {"version": 2, "ok": True, "page": page.to_json()}
        if len(encode_frame(response)) - 4 > CONTROL_LIST_RESPONSE_MAX_BYTES:
            raise IpcError("control list page exceeds page budget", reason_code="item_too_large")
        return response

    @staticmethod
    def _program_status_payload(status: object) -> dict[str, object]:
        """Project a program decision without manufacturing a dispatch id."""

        if not isinstance(status, ProgramControllerDecisionStatus):
            raise IpcError("program decision status is not typed")
        payload = thaw_json(status.payload)
        if not isinstance(payload, dict):
            raise IpcError("program decision payload is not JSON")
        return {
            "decision_id": str(status.decision_id),
            "program_id": str(status.program_id),
            "event_kind": status.event_kind.value,
            "event_key": status.event_key,
            "state": status.state.value,
            "revision": status.revision,
            "current_generation": int(status.current_generation),
            "generation_budget": status.generation_budget,
            "generation_used": status.generation_used,
            "claimant_kind": status.claimant_kind.value if status.claimant_kind else None,
            "claimant_id": status.claimant_id,
            "claim_expires_at": status.claim_expires_at,
            "action_id": status.action_id,
            "action_sha256": status.action_sha256,
            "deadline": status.deadline,
            "payload": payload,
        }

    @staticmethod
    def _controller_claim_payload(claim: object) -> dict[str, object]:
        from .domain import ControllerDecisionClaim

        if not isinstance(claim, ControllerDecisionClaim):
            raise IpcError("controller decision claim is not typed")
        return {
            "decision_id": str(claim.decision_id),
            "generation": int(claim.generation),
            "claimant_kind": claim.claimant_kind.value,
            "claimant_id": claim.claimant_id,
            "revision": claim.revision,
            "lease_expires_at": claim.lease_expires_at,
            "token": claim.token,
        }

    @staticmethod
    def _controller_generation_payload(generation: object) -> dict[str, object]:
        from .domain import ControllerGenerationStatus

        if not isinstance(generation, ControllerGenerationStatus):
            raise IpcError("controller generation status is not typed")
        return {
            "decision_id": str(generation.decision_id),
            "generation": int(generation.generation),
            "lineage_id": generation.lineage_id,
            "predecessor_generation": int(generation.predecessor_generation)
            if generation.predecessor_generation is not None
            else None,
            "source_kind": generation.source_kind,
            "state": generation.state.value,
            "prompt_sha256": generation.prompt_sha256,
            "controller_thread_id": generation.controller_thread_id.id
            if generation.controller_thread_id is not None
            else None,
            "controller_turn_id": generation.controller_turn_id,
            "inspection_outcome": generation.inspection_outcome,
        }

    @staticmethod
    def _controller_inspection_claim_payload(claim: object) -> dict[str, object]:
        if not isinstance(claim, ControllerRecoveryInspectionClaim):
            raise IpcError("controller inspection claim is not typed")
        return {
            "decision_id": str(claim.decision_id),
            "generation": int(claim.generation),
            "revision": claim.revision,
            "lease_expires_at": claim.lease_expires_at,
            "token": claim.token,
        }

    @staticmethod
    def _controller_receipt_payload(receipt: object) -> dict[str, object]:
        from .domain import ControllerActionReceipt

        if not isinstance(receipt, ControllerActionReceipt):
            raise IpcError("controller action receipt is not typed")
        return {
            "action_id": receipt.action_id,
            "decision_id": str(receipt.decision_id),
            "generation": int(receipt.generation),
            "expected_revision": receipt.expected_revision,
            "bundle_sha256": receipt.bundle_sha256,
            "effect_receipt": receipt.effect_receipt,
            "state": receipt.state,
            "committed_at": receipt.committed_at,
            "acknowledged_at": receipt.acknowledged_at,
        }

    def _controller_request(self, payload: dict[str, object]) -> dict[str, object]:
        operation = payload.get("operation")
        if operation == "program_pending":
            if set(payload) != {"version", "operation"}:
                raise IpcError("program pending request has an unsupported shape")
            return {
                "version": 1,
                "ok": True,
                "decisions": [
                    self._program_status_payload(item) for item in self.ledger.program_controller_decisions()
                ],
            }
        if operation == "program_status":
            if set(payload) != {"version", "operation", "decision_id"}:
                raise IpcError("program status request has an unsupported shape")
            status = self.ledger.program_controller_decision(ControllerDecisionId(payload["decision_id"]))
            return {"version": 1, "ok": True, "decision": self._program_status_payload(status)}
        if operation == "program_start":
            required = {"version", "operation", "program_id", "event_key"}
            if set(payload) != required:
                raise IpcError("program start request has an unsupported shape")
            program_id = payload["program_id"]
            event_key = payload["event_key"]
            if not isinstance(program_id, str) or not isinstance(event_key, str):
                raise IpcError("program start identity is invalid")
            from .controller import Controller

            controller = Controller(self.state_root, worktrees=self._worktrees)
            try:
                decision = controller.start_program(program_id, event_key=event_key)
            finally:
                controller.close()
            return {"version": 1, "ok": True, "decision": self._program_status_payload(decision)}
        if operation == "program_context":
            required = {
                "version",
                "operation",
                "program_id",
                "expected_revision",
                "expected_trunk_head",
            }
            if set(payload) != required:
                raise IpcError("program context request has an unsupported shape")
            context = self.ledger.program_controller_context(
                payload["program_id"],
                expected_revision=payload["expected_revision"],
                expected_trunk_head=payload["expected_trunk_head"],
            )
            return {"version": 1, "ok": True, "context": context.to_json()}
        if operation == "program_claim":
            allowed = {
                "version",
                "operation",
                "decision_id",
                "claimant_kind",
                "claimant_id",
                "expected_revision",
                "generation",
            }
            required = {"version", "operation", "decision_id", "claimant_kind", "claimant_id", "expected_revision"}
            if set(payload) - allowed or not required.issubset(payload):
                raise IpcError("program claim request has an unsupported shape")
            claim = self.ledger.claim_program_controller_decision(
                ControllerDecisionId(payload["decision_id"]),
                claimant_kind=payload["claimant_kind"],
                claimant_id=payload["claimant_id"],
                expected_revision=payload["expected_revision"],
                generation=payload.get("generation"),
            )
            return {"version": 1, "ok": True, "claim": self._controller_claim_payload(claim)}
        if operation == "program_submit_actions":
            required = {"version", "operation", "bundle", "claimant_id", "token"}
            if set(payload) != required or not isinstance(payload["bundle"], dict):
                raise IpcError("program action submission has an unsupported shape")
            bundle = ModelFacingProgramControllerActionBundle.from_json(payload["bundle"])
            graph = self.ledger.program_graph(bundle.program_id)
            for action in bundle.actions:
                if action.kind not in {
                    ProgramControllerActionKind.PROMOTE_CANDIDATE,
                    ProgramControllerActionKind.INTEGRATE_CANDIDATE,
                }:
                    continue
                if action.milestone_id is None or action.candidate_sha is None:
                    raise IpcError("program candidate action is incomplete")
                try:
                    self._worktrees.validate_candidate(
                        graph.node(action.milestone_id).capsule,
                        action.candidate_sha,
                    )
                except (CandidateIntegrityError, WorktreeError, ValueError) as exc:
                    raise IpcError("program candidate integrity validation failed") from exc
            receipt = self.ledger.submit_program_controller_actions(
                bundle,
                claimant_id=payload["claimant_id"],
                token=payload["token"],
            )
            try:
                self._apply_program_action_bundle(bundle)
            except RuntimeError as exc:
                raise IpcError("program action effect failed") from exc
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "program_submit_recovered_actions":
            required = {"version", "operation", "decision_id"}
            if set(payload) != required:
                raise IpcError("recovered program action submission has an unsupported shape")
            identity = ControllerDecisionId(payload["decision_id"])
            receipt = self.ledger.submit_recovered_program_controller_actions(identity)
            try:
                self._apply_program_action_outbox(receipt.action_id)
            except RuntimeError as exc:
                raise IpcError("program action effect failed") from exc
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "program_acknowledge":
            required = {
                "version",
                "operation",
                "decision_id",
                "action_id",
                "bundle_sha256",
                "committed_revision",
                "claimant_id",
                "token",
            }
            if set(payload) != required:
                raise IpcError("program acknowledgement has an unsupported shape")
            receipt = self.ledger.acknowledge_controller_action(
                ControllerDecisionId(payload["decision_id"]),
                action_id=payload["action_id"],
                bundle_sha256=payload["bundle_sha256"],
                committed_revision=payload["committed_revision"],
                claimant_id=payload["claimant_id"],
                token=payload["token"],
            )
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "program_acknowledge_recovered":
            required = {
                "version",
                "operation",
                "decision_id",
                "action_id",
                "bundle_sha256",
                "committed_revision",
            }
            if set(payload) != required:
                raise IpcError("recovered program acknowledgement has an unsupported shape")
            receipt = self.ledger.acknowledge_recovered_controller_action(
                ControllerDecisionId(payload["decision_id"]),
                action_id=payload["action_id"],
                bundle_sha256=payload["bundle_sha256"],
                committed_revision=payload["committed_revision"],
            )
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "controller_pending":
            if set(payload) != {"version", "operation"}:
                raise IpcError("controller pending request has an unsupported shape")
            return {
                "version": 1,
                "ok": True,
                "decisions": [self._controller_status_payload(item) for item in self.ledger.controller_decisions()],
            }
        if operation == "controller_status":
            if set(payload) != {"version", "operation", "decision_id"}:
                raise IpcError("controller status request has an unsupported shape")
            status = self.ledger.controller_decision(ControllerDecisionId(payload["decision_id"]))
            return {"version": 1, "ok": True, "decision": self._controller_status_payload(status)}
        if operation == "controller_claim":
            allowed = {
                "version",
                "operation",
                "decision_id",
                "claimant_kind",
                "claimant_id",
                "expected_revision",
                "generation",
                "token",
            }
            if set(payload) - allowed or not {
                "version",
                "operation",
                "decision_id",
                "claimant_kind",
                "claimant_id",
                "expected_revision",
            }.issubset(payload):
                raise IpcError("controller claim request has an unsupported shape")
            claim = self.ledger.claim_controller_decision(
                ControllerDecisionId(payload["decision_id"]),
                claimant_kind=payload["claimant_kind"],
                claimant_id=payload["claimant_id"],
                expected_revision=payload["expected_revision"],
                generation=payload.get("generation"),
                token=payload.get("token"),
            )
            return {"version": 1, "ok": True, "claim": self._controller_claim_payload(claim)}
        if operation == "controller_renew_claim":
            required = {"version", "operation", "decision_id", "claimant_id", "token", "expected_revision"}
            if set(payload) != required:
                raise IpcError("controller claim renewal request has an unsupported shape")
            claim = self.ledger.renew_controller_decision_claim(
                ControllerDecisionId(payload["decision_id"]),
                claimant_id=payload["claimant_id"],
                token=payload["token"],
                expected_revision=payload["expected_revision"],
            )
            return {"version": 1, "ok": True, "claim": self._controller_claim_payload(claim)}
        if operation == "controller_defer_rate_limit":
            required = {
                "version",
                "operation",
                "decision_id",
                "generation",
                "claimant_id",
                "token",
                "expected_revision",
                "retry_at",
            }
            if set(payload) != required:
                raise IpcError("controller rate-limit deferral request has an unsupported shape")
            claim = self.ledger.defer_controller_rate_limit(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                claimant_id=payload["claimant_id"],
                token=payload["token"],
                expected_revision=payload["expected_revision"],
                retry_at=payload["retry_at"],
            )
            return {"version": 1, "ok": True, "claim": self._controller_claim_payload(claim)}
        if operation == "controller_submit_actions":
            required = {"version", "operation", "bundle", "claimant_id", "token"}
            if set(payload) != required or not isinstance(payload["bundle"], dict):
                raise IpcError("controller action submission has an unsupported shape")
            bundle = ModelFacingControllerActionBundle.from_json(payload["bundle"])
            receipt = self.ledger.submit_controller_actions(
                bundle, claimant_id=payload["claimant_id"], token=payload["token"]
            )
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "controller_submit_recovered_actions":
            allowed = {"version", "operation", "bundle", "decision_id"}
            if set(payload) - allowed or not ({"version", "operation"} <= set(payload)):
                raise IpcError("recovered controller action submission has an unsupported shape")
            if ("bundle" in payload) == ("decision_id" in payload):
                raise IpcError("recovered controller action submission requires one decision identity")
            argument: object
            if "bundle" in payload:
                if not isinstance(payload["bundle"], dict):
                    raise IpcError("recovered controller action bundle is malformed")
                argument = ModelFacingControllerActionBundle.from_json(payload["bundle"])
            else:
                argument = ControllerDecisionId(payload["decision_id"])
            receipt = self.ledger.submit_recovered_controller_actions(argument)
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "controller_acknowledge":
            required = {
                "version",
                "operation",
                "decision_id",
                "action_id",
                "bundle_sha256",
                "committed_revision",
                "claimant_id",
                "token",
            }
            if set(payload) != required:
                raise IpcError("controller acknowledgement has an unsupported shape")
            receipt = self.ledger.acknowledge_controller_action(
                ControllerDecisionId(payload["decision_id"]),
                action_id=payload["action_id"],
                bundle_sha256=payload["bundle_sha256"],
                committed_revision=payload["committed_revision"],
                claimant_id=payload["claimant_id"],
                token=payload["token"],
            )
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "controller_acknowledge_recovered":
            required = {
                "version",
                "operation",
                "decision_id",
                "action_id",
                "bundle_sha256",
                "committed_revision",
            }
            if set(payload) != required:
                raise IpcError("recovered controller acknowledgement has an unsupported shape")
            receipt = self.ledger.acknowledge_recovered_controller_action(
                ControllerDecisionId(payload["decision_id"]),
                action_id=payload["action_id"],
                bundle_sha256=payload["bundle_sha256"],
                committed_revision=payload["committed_revision"],
            )
            return {"version": 1, "ok": True, "receipt": self._controller_receipt_payload(receipt)}
        if operation == "controller_generation":
            allowed = {"version", "operation", "decision_id", "generation"}
            if set(payload) - allowed or not {"version", "operation", "decision_id"}.issubset(payload):
                raise IpcError("controller generation request has an unsupported shape")
            generation = self.ledger.controller_generation(
                ControllerDecisionId(payload["decision_id"]),
                payload.get("generation"),
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_prepare_generation":
            allowed = {"version", "operation", "decision_id", "generation", "prompt_sha256"}
            if set(payload) - allowed or not {"version", "operation", "decision_id", "generation"}.issubset(payload):
                raise IpcError("controller generation preparation has an unsupported shape")
            generation = self.ledger.prepare_controller_generation(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                prompt_sha256=payload.get("prompt_sha256"),
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_bind_generation_thread":
            required = {"version", "operation", "decision_id", "generation", "controller_thread_id"}
            if set(payload) != required:
                raise IpcError("controller generation thread binding has an unsupported shape")
            generation = self.ledger.bind_controller_generation_thread(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                controller_thread_id=payload["controller_thread_id"],
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_bind_generation_turn":
            required = {
                "version",
                "operation",
                "decision_id",
                "generation",
                "controller_thread_id",
                "controller_turn_id",
            }
            if set(payload) - (required | {"previous_controller_turn_id"}) or not required.issubset(payload):
                raise IpcError("controller generation turn binding has an unsupported shape")
            generation = self.ledger.bind_controller_generation_turn(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                controller_thread_id=payload["controller_thread_id"],
                controller_turn_id=payload["controller_turn_id"],
                previous_controller_turn_id=payload.get("previous_controller_turn_id"),
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_complete_generation":
            required = {"version", "operation", "decision_id", "generation", "state"}
            if set(payload) != required:
                raise IpcError("controller generation completion has an unsupported shape")
            generation = self.ledger.complete_controller_generation(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                state=ControllerGenerationState(payload["state"]),
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_reset_generation_delivery":
            required = {
                "version",
                "operation",
                "decision_id",
                "generation",
                "expected_revision",
                "claimant_id",
                "token",
            }
            if set(payload) != required:
                raise IpcError("controller generation reset has an unsupported shape")
            generation = self.ledger.reset_controller_generation_delivery(
                ControllerDecisionId(payload["decision_id"]),
                generation=payload["generation"],
                expected_revision=payload["expected_revision"],
                claimant_id=payload["claimant_id"],
                token=payload["token"],
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation == "controller_recover":
            required = {"version", "operation", "decision_id", "inspection_outcome"}
            if set(payload) != required:
                raise IpcError("controller recovery request has an unsupported shape")
            generation = self.ledger.request_controller_recovery(
                ControllerDecisionId(payload["decision_id"]), inspection_outcome=payload["inspection_outcome"]
            )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        if operation in {"controller_reserve_recovery", "controller_complete_recovery"}:
            required = {"version", "operation", "decision_id"}
            if operation == "controller_complete_recovery":
                required.update({"inspection_outcome", "claim"})
            if set(payload) - (required | {"bundle"}) or not required.issubset(payload):
                raise IpcError("controller recovery inspection request has an unsupported shape")
            identity = ControllerDecisionId(payload["decision_id"])
            if operation == "controller_reserve_recovery":
                claim = self.ledger.reserve_controller_recovery_inspection(identity)
                return {"version": 1, "ok": True, "claim": self._controller_inspection_claim_payload(claim)}
            else:
                raw_claim = payload["claim"]
                if not isinstance(raw_claim, dict) or set(raw_claim) != {
                    "decision_id",
                    "generation",
                    "revision",
                    "lease_expires_at",
                    "token",
                }:
                    raise IpcError("controller inspection claim is malformed")
                claim = ControllerRecoveryInspectionClaim(
                    ControllerDecisionId(raw_claim["decision_id"]),
                    raw_claim["generation"],  # type: ignore[arg-type]
                    raw_claim["revision"],  # type: ignore[arg-type]
                    raw_claim["lease_expires_at"],  # type: ignore[arg-type]
                    raw_claim["token"],  # type: ignore[arg-type]
                )
                generation = self.ledger.complete_controller_recovery_inspection(
                    identity,
                    inspection_outcome=payload["inspection_outcome"],
                    claim=claim,
                    bundle=ModelFacingControllerActionBundle.from_json(payload["bundle"])
                    if isinstance(payload.get("bundle"), dict)
                    else None,
                )
            return {"version": 1, "ok": True, "generation": self._controller_generation_payload(generation)}
        raise IpcError("unsupported controller operation")

    def _request_ack(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            operation = payload.get("operation")
            if not isinstance(operation, str):
                raise IpcError("unsupported IPC request version or operation")
            if payload.get("version") == 2 and operation in {"status", "controller_pending"}:
                if set(payload) != {"version", "operation", "list_kind", "visibility", "page_token", "page_items"}:
                    raise IpcError("control list request has an unsupported shape")
                expected_kind = ControlListKind.WORKERS if operation == "status" else ControlListKind.DECISIONS
                try:
                    request = ControlListRequest(
                        ControlListKind(payload["list_kind"]),  # type: ignore[arg-type]
                        ControlListVisibility(payload["visibility"]),  # type: ignore[arg-type]
                        payload["page_token"],  # type: ignore[arg-type]
                        payload["page_items"],  # type: ignore[arg-type]
                    )
                except (TypeError, ValueError) as exc:
                    reason = "malformed_page_token" if payload.get("page_token") is not None else None
                    raise IpcError("control list request is malformed", reason_code=reason) from exc
                if request.list_kind is not expected_kind:
                    raise IpcError(
                        "control list operation identity does not match", reason_code="page_token_identity_mismatch"
                    )
                return self._control_list_page(request)
            if payload.get("version") != 1:
                raise IpcError("unsupported IPC request version or operation")
            if operation == "wake":
                if set(payload) != {"version", "operation"}:
                    raise IpcError("wake request has an unsupported shape")
                return {"version": 1, "ok": True, "operation": "wake"}
            if operation == "shutdown":
                if set(payload) != {"version", "operation"}:
                    raise IpcError("shutdown request has an unsupported shape")
                if self.epoch is None:
                    raise IpcError("harness lease is not acquired")
                self.ledger.request_harness_shutdown(epoch=self.epoch, owner_nonce_sha256=self.owner_nonce_sha256)
                self._stop = True
                return {"version": 1, "ok": True, "operation": "shutdown"}
            if operation.startswith(("controller_", "program_")):
                return self._controller_request(payload)
            if operation == "status":
                if set(payload) - {"version", "operation", "dispatch_id"}:
                    raise IpcError("status request has an unsupported shape")
                selected = payload.get("dispatch_id")
                rows = self.ledger.queue_dispatches()
                if selected is not None:
                    if not isinstance(selected, str):
                        raise IpcError("status dispatch identity is invalid")
                    rows = tuple(row for row in rows if row.get("dispatch_id") == selected)
                queue = []
                for row in rows:
                    queue.append(self._status_page_item(row))
                return {"version": 1, "ok": True, "queue": queue}
            if operation == "activity":
                return self._activity(payload)
            if operation == "conversation_history":
                return self._conversation_history(payload)
            if operation == "conversation_history_response":
                return self._history_response(payload)
            if operation == "live_keyframe":
                return self._publish_live_keyframe(payload)
            if operation == "control_status":
                if set(payload) != {"version", "operation", "command_id"}:
                    raise IpcError("control status request has an unsupported shape")
                command_id = payload.get("command_id")
                if not isinstance(command_id, str):
                    raise IpcError("control command id is invalid")
                try:
                    command = self.ledger.control_command(command_id)
                except RecordNotFound:
                    command = None
                return {
                    "version": 1,
                    "ok": True,
                    "command": None if command is None else _public_control_command(command),
                }
            if operation == "steer":
                return self._create_control(payload, ControlCommandKind.STEER)
            if operation == "interrupt":
                return self._create_control(payload, ControlCommandKind.INTERRUPT)
            if operation == "control":
                kind_value = payload.get("kind")
                try:
                    kind = ControlCommandKind(str(kind_value))
                except ValueError as exc:
                    raise IpcError("control command kind is invalid") from exc
                normalized = dict(payload)
                normalized["operation"] = kind.value
                normalized.pop("kind", None)
                return self._create_control(normalized, kind)
            if operation == "poll_commands":
                return self._poll_commands(payload)
            if operation == "ack_control":
                return self._ack_control(payload)
            if operation == "worker_event":
                return self._worker_event(payload)
            if operation == "bind_turn":
                return self._bind_turn(payload)
            if operation == "retry":
                return self._retry(payload)
            if operation == "recovery_action":
                return self._recovery_action(payload)
            if operation == "bind_worker":
                return self._bind_worker(payload)
            if operation == "heartbeat":
                return self._heartbeat(payload)
            if operation == "submit_result":
                return self._submit_result(payload)
            raise IpcError("unsupported IPC operation")
        except (KeyError, TypeError, ValueError, LedgerError) as exc:
            raise IpcError("IPC request failed closed") from exc

    @staticmethod
    def _strict_worker_process_identity(payload: dict[str, object], row: dict[str, object]) -> tuple[int, int]:
        generation = payload.get("generation")
        attempt = payload.get("attempt")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise IpcError("worker generation is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise IpcError("worker attempt is invalid")
        if (generation, attempt) != (row.get("generation"), row.get("attempt")):
            raise IpcError("worker process identity is stale")
        return generation, attempt

    @classmethod
    def _strict_worker_turn_identity(
        cls, payload: dict[str, object], row: dict[str, object]
    ) -> tuple[int, int, str, str]:
        generation, attempt = cls._strict_worker_process_identity(payload, row)
        thread_id = payload.get("thread_id")
        turn_id = payload.get("turn_id")
        if not isinstance(thread_id, str) or row.get("thread_id") != thread_id:
            raise IpcError("worker turn thread identity conflicts with queue")
        try:
            ThreadIdentity(thread_id)
        except ValueError as exc:
            raise IpcError("worker turn thread identity is invalid") from exc
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or len(turn_id) > 512
            or any(character.isspace() or ord(character) < 0x20 for character in turn_id)
        ):
            raise IpcError("worker turn identity is invalid")
        return generation, attempt, thread_id, turn_id

    @staticmethod
    def _control_identity(payload: dict[str, object]) -> tuple[str, int, int, str, str, str]:
        dispatch_id = payload.get("dispatch_id")
        thread_id = payload.get("thread_id")
        turn_id = payload.get("turn_id")
        if not isinstance(dispatch_id, str) or not isinstance(thread_id, str) or not isinstance(turn_id, str):
            raise IpcError("live control identity is incomplete")
        try:
            ThreadIdentity(thread_id)
        except ValueError as exc:
            raise IpcError("live control thread identity is invalid") from exc
        if (
            not turn_id
            or len(turn_id) > 512
            or any(character.isspace() or ord(character) < 0x20 for character in turn_id)
        ):
            raise IpcError("live control turn identity is invalid")
        generation = payload.get("generation")
        attempt = payload.get("attempt")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise IpcError("live control generation is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise IpcError("live control attempt is invalid")
        command_id = payload.get("command_id")
        if (
            not isinstance(command_id, str)
            or not command_id.strip()
            or len(command_id.encode("utf-8")) > 256
            or any(character.isspace() or ord(character) < 0x20 for character in command_id)
        ):
            raise IpcError("live control command id is invalid")
        return dispatch_id, generation, attempt, thread_id, turn_id, command_id

    def _activity(self, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) - {"version", "operation", "dispatch_id", "limit"}:
            raise IpcError("activity request has an unsupported shape")
        dispatch_id = payload.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            raise IpcError("activity dispatch identity is required")
        limit = payload.get("limit", 128)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 128:
            raise IpcError("activity limit is invalid")
        return {
            "version": 1,
            "ok": True,
            "dispatch_id": dispatch_id,
            "activity": [
                _public_activity(activity) for activity in self.ledger.recent_activity(dispatch_id, limit=limit)
            ],
        }

    def _live_subscription(self, payload: dict[str, object]) -> _LiveSubscriber:
        required = {"version", "operation", "dispatch_id", "generation", "attempt", "thread_id", "turn_id"}
        if set(payload) != required:
            raise IpcError("live subscription request has an unsupported shape")
        dispatch_id = payload.get("dispatch_id")
        thread_id = payload.get("thread_id")
        turn_id = payload.get("turn_id")
        generation = payload.get("generation")
        attempt = payload.get("attempt")
        if not isinstance(dispatch_id, str) or not isinstance(thread_id, str) or not isinstance(turn_id, str):
            raise IpcError("live subscription identity is incomplete")
        try:
            DispatchId(dispatch_id)
            ThreadIdentity(thread_id)
        except ValueError as exc:
            raise IpcError("live subscription identity is invalid") from exc
        if (
            not turn_id
            or any(character.isspace() or ord(character) < 0x20 for character in turn_id)
            or len(turn_id.encode("utf-8")) > 512
        ):
            raise IpcError("live subscription turn identity is invalid")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise IpcError("live subscription generation is invalid")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise IpcError("live subscription attempt is invalid")
        row = self.ledger.queue_dispatch(dispatch_id)
        active = getattr(self, "_active_turns", {}).get(dispatch_id)
        if (
            row.get("state") not in {"starting", "running"}
            or active != (generation, attempt, thread_id, turn_id)
            or row.get("generation") != generation
            or row.get("attempt") != attempt
            or row.get("thread_id") != thread_id
        ):
            raise IpcError("live subscription identity is stale")
        lock, subscribers = self._live_state()
        with lock:
            if len(subscribers) >= LIVE_MAX_SUBSCRIBERS:
                raise IpcError("live subscription capacity is busy")
            subscriber = _LiveSubscriber(dispatch_id, generation, attempt, thread_id, turn_id)
            subscribers[subscriber.subscription_id] = subscriber
        return subscriber

    def _live_state(self) -> tuple[threading.Lock, dict[str, _LiveSubscriber]]:
        """Lazily initialize the in-memory broker for hermetic harness fixtures."""

        lock = getattr(self, "_live_subscribers_lock", None)
        subscribers = getattr(self, "_live_subscribers", None)
        if lock is None:
            lock = threading.Lock()
            self._live_subscribers_lock = lock
        if subscribers is None:
            subscribers = {}
            self._live_subscribers = subscribers
        if getattr(self, "_live_revisions", None) is None:
            self._live_revisions: dict[tuple[str, int, int, str, str], int] = {}
        return lock, subscribers

    def _close_live_subject(
        self, dispatch_id: str | None = None, identity: tuple[int, int, str, str] | None = None
    ) -> None:
        """Wake matching subscribers so terminal/replacement state is not replayed."""

        lock, subscribers = self._live_state()
        with lock:
            for subscriber in subscribers.values():
                if dispatch_id is not None and subscriber.dispatch_id != dispatch_id:
                    continue
                current = (subscriber.generation, subscriber.attempt, subscriber.thread_id, subscriber.turn_id)
                if identity is not None and current != identity:
                    continue
                subscriber.terminal = True
                while True:
                    try:
                        subscriber.frames.get_nowait()
                    except queue.Empty:
                        break
                subscriber.wake.set()
            revisions = self._live_revisions
            for subject in tuple(revisions):
                subject_dispatch, subject_generation, subject_attempt, subject_thread, subject_turn = subject
                if dispatch_id is not None and subject_dispatch != dispatch_id:
                    continue
                if (
                    identity is not None
                    and (subject_generation, subject_attempt, subject_thread, subject_turn) != identity
                ):
                    continue
                revisions.pop(subject, None)

    def _publish_live_keyframe(self, payload: dict[str, object]) -> dict[str, object]:
        required = {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "token",
            "thread_id",
            "turn_id",
            "keyframe",
        }
        if set(payload) != required:
            raise IpcError("live keyframe request has an unsupported shape")
        row, generation, attempt, _token = self._worker_capability(payload)
        generation, attempt, thread_id, turn_id = self._strict_worker_turn_identity(payload, row)
        active = getattr(self, "_active_turns", {}).get(str(row["dispatch_id"]))
        if active != (generation, attempt, thread_id, turn_id):
            raise IpcError("live keyframe turn identity is stale")
        try:
            keyframe = LiveTurnKeyframe.from_json(payload["keyframe"])
        except (TypeError, ValueError) as exc:
            raise IpcError("live keyframe is malformed") from exc
        if (
            str(keyframe.dispatch_id) != str(row["dispatch_id"])
            or keyframe.generation != generation
            or keyframe.attempt != attempt
            or keyframe.thread_id.id != thread_id
            or keyframe.turn_id != turn_id
        ):
            raise IpcError("live keyframe identity is stale")
        envelope = {"version": 1, "ok": True, "event": "keyframe", "keyframe": keyframe.to_json()}
        try:
            encode_frame(envelope)
        except IpcError as exc:
            raise IpcError("live keyframe exceeds its bounded frame") from exc
        delivered = 0
        lock, subscriber_registry = self._live_state()
        with lock:
            subject = (str(row["dispatch_id"]), generation, attempt, thread_id, turn_id)
            if keyframe.live_revision <= self._live_revisions.get(subject, 0):
                return {"version": 1, "ok": True, "operation": "live_keyframe", "delivered": 0}
            self._live_revisions[subject] = keyframe.live_revision
            subscribers = tuple(subscriber_registry.values())
            for subscriber in subscribers:
                if (
                    subscriber.dispatch_id,
                    subscriber.generation,
                    subscriber.attempt,
                    subscriber.thread_id,
                    subscriber.turn_id,
                ) != (str(row["dispatch_id"]), generation, attempt, thread_id, turn_id):
                    continue
                try:
                    subscriber.frames.put_nowait(keyframe)
                except queue.Full:
                    try:
                        subscriber.frames.get_nowait()
                        subscriber.frames.put_nowait(keyframe)
                    except queue.Empty:
                        continue
                subscriber.wake.set()
                delivered += 1
        return {"version": 1, "ok": True, "operation": "live_keyframe", "delivered": delivered}

    def _serve_live_connection(self, connection: socket.socket, subscriber: _LiveSubscriber) -> None:
        """Push bounded cumulative frames without blocking harness lifecycle work."""

        try:
            connection.settimeout(0.25)
            try:
                connection.sendall(encode_frame({"version": 1, "ok": True, "operation": "live_subscribe"}))
            except OSError:
                return
            # Frames accumulated before the socket handshake are stale
            # ephemeral state.  Reconnect/terminal recovery comes from the
            # authoritative Thread.read history, so never replay this queue.
            while True:
                try:
                    subscriber.frames.get_nowait()
                except queue.Empty:
                    break
            while True:
                if subscriber.terminal:
                    terminal = {
                        "version": 1,
                        "ok": True,
                        "event": "terminal",
                        "dispatch_id": subscriber.dispatch_id,
                        "generation": subscriber.generation,
                        "attempt": subscriber.attempt,
                        "thread_id": subscriber.thread_id,
                        "turn_id": subscriber.turn_id,
                    }
                    try:
                        connection.sendall(encode_frame(terminal))
                    except OSError:
                        pass
                    return
                try:
                    keyframe = subscriber.frames.get_nowait()
                except queue.Empty:
                    subscriber.wake.wait()
                    subscriber.wake.clear()
                    continue
                if subscriber.terminal:
                    continue
                try:
                    connection.sendall(
                        encode_frame({"version": 1, "ok": True, "event": "keyframe", "keyframe": keyframe.to_json()})
                    )
                except (IpcError, OSError):
                    return
        finally:
            lock, subscribers = self._live_state()
            with lock:
                subscribers.pop(subscriber.subscription_id, None)
            connection.close()

    @staticmethod
    def _public_conversation_page(page: ConversationHistoryPage) -> dict[str, object]:
        return {"version": 1, "ok": True, "page": page.to_json()}

    @staticmethod
    def _conversation_request(payload: dict[str, object]) -> ConversationHistoryRequest:
        required = {
            "version",
            "operation",
            "subject_kind",
            "subject_id",
            "thread_id",
            "generation",
            "attempt",
            "revision",
            "page_token",
            "page_fragments",
        }
        if set(payload) != required:
            raise IpcError("conversation history request has an unsupported shape")
        try:
            return ConversationHistoryRequest(
                ConversationSubjectKind(payload["subject_kind"]),
                payload["subject_id"],  # type: ignore[arg-type]
                ThreadIdentity(payload["thread_id"]),  # type: ignore[arg-type]
                payload["generation"],  # type: ignore[arg-type]
                payload["attempt"],  # type: ignore[arg-type]
                payload["revision"],  # type: ignore[arg-type]
                payload["page_token"],  # type: ignore[arg-type]
                payload["page_fragments"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise IpcError("conversation history request is malformed") from exc

    def _inactive_conversation_page(self, request: ConversationHistoryRequest) -> ConversationHistoryPage:
        if request.subject_kind is ConversationSubjectKind.WORKER:
            try:
                row = self.ledger.queue_dispatch(request.subject_id)
            except (LedgerError, RecordNotFound):
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.DELETED,
                    None,
                    reason="worker history is deleted",
                )
            if row.get("thread_id") != request.thread_id.id:
                raise IpcError("conversation worker thread identity is stale")
            if request.generation != row.get("generation") or request.attempt != row.get("attempt"):
                raise IpcError("conversation worker process identity is stale")
            model = "gpt-5.6-luna"
            effort = ReasoningEffort.MEDIUM
            try:
                capsule = strict_json_loads(str(row["capsule_json"]), max_bytes=2_000_000)
                if isinstance(capsule, dict):
                    if isinstance(capsule.get("model"), str):
                        model = capsule["model"]
                    if isinstance(capsule.get("reasoning_effort"), str):
                        effort = ReasoningEffort(capsule["reasoning_effort"])
            except (TypeError, ValueError, KeyError):
                pass
            config = CodexSdkConfig(model, effort, sandbox=Sandbox.READ_ONLY, cwd=Path(str(row["workspace_path"])))
        else:
            try:
                decision = self.ledger.controller_decision(ControllerDecisionId(request.subject_id))
            except (LedgerError, ValueError):
                return ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.DELETED,
                    None,
                    reason="controller history is deleted",
                )
            source = decision.summary.source_thread_id
            if source != request.thread_id.id:
                raise IpcError("conversation controller thread identity is stale")
            if request.generation != int(decision.current_generation) or request.revision != decision.revision:
                raise IpcError("conversation controller revision is stale")
            config = CodexSdkConfig(
                "gpt-5.6-luna", ReasoningEffort.MEDIUM, sandbox=Sandbox.READ_ONLY, cwd=self.state_root
            )
        adapter = CodexSdkAdapter(config)
        try:
            return adapter.read_conversation_history(request)
        finally:
            adapter.close()

    def _history_response(self, payload: dict[str, object]) -> dict[str, object]:
        required = {
            "version",
            "operation",
            "request_id",
            "dispatch_id",
            "generation",
            "attempt",
            "thread_id",
            "turn_id",
            "token",
            "page",
        }
        if set(payload) != required:
            raise IpcError("conversation history response has an unsupported shape")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            raise IpcError("conversation history response identity is invalid")
        with self._conversation_requests_lock:
            pending = self._conversation_requests.get(request_id)
        if pending is None:
            raise IpcError("conversation history response is stale")
        dispatch_id = str(pending["dispatch_id"])
        row, generation, attempt, _token = self._worker_capability(payload)
        if dispatch_id != str(row["dispatch_id"]) or (generation, attempt) != (
            pending["generation"],
            pending["attempt"],
        ):
            raise IpcError("conversation history response process identity is stale")
        if payload.get("thread_id") != pending["thread_id"] or payload.get("turn_id") != pending["turn_id"]:
            raise IpcError("conversation history response turn identity is stale")
        page = payload.get("page")
        try:
            typed_page = conversation_history_page_from_json(page)
        except (TypeError, ValueError) as exc:
            raise IpcError("conversation history response page is malformed") from exc
        request = pending["request"]
        assert isinstance(request, ConversationHistoryRequest)
        if (
            typed_page.subject_kind is not request.subject_kind
            or typed_page.subject_id != request.subject_id
            or typed_page.thread_id != request.thread_id
        ):
            raise IpcError("conversation history response page is malformed")
        pending["response"] = typed_page
        event = pending["event"]
        assert isinstance(event, threading.Event)
        event.set()
        return {"version": 1, "ok": True}

    def _conversation_history(self, payload: dict[str, object]) -> dict[str, object]:
        request = self._conversation_request(payload)
        active = getattr(self, "_active_turns", {}).get(request.subject_id)
        if request.subject_kind is ConversationSubjectKind.WORKER and active is not None:
            if (request.generation, request.attempt, request.thread_id.id) != active[:3]:
                raise IpcError("conversation worker identity is stale")
        if not self._conversation_read_slots.acquire(blocking=False):
            return self._public_conversation_page(
                ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.UNAVAILABLE,
                    None,
                    reason="conversation read capacity is busy",
                )
            )
        if request.subject_kind is ConversationSubjectKind.WORKER and active is not None:
            request_id = uuid.uuid4().hex
            event = threading.Event()
            pending: dict[str, object] = {
                "request": request,
                "event": event,
                "dispatch_id": request.subject_id,
                "generation": request.generation,
                "attempt": request.attempt,
                "thread_id": request.thread_id.id,
                "turn_id": active[3],
            }
            with self._conversation_requests_lock:
                self._conversation_requests[request_id] = pending
            try:
                if not event.wait(CONVERSATION_READ_DEADLINE_SECONDS):
                    page = ConversationHistoryPage(
                        request.subject_kind,
                        request.subject_id,
                        request.thread_id,
                        ConversationHistoryStatus.UNAVAILABLE,
                        None,
                        reason="active worker did not answer history read",
                    )
                else:
                    typed_page = pending.get("response")
                    if not isinstance(typed_page, ConversationHistoryPage):
                        page = ConversationHistoryPage(
                            request.subject_kind,
                            request.subject_id,
                            request.thread_id,
                            ConversationHistoryStatus.UNAVAILABLE,
                            None,
                            reason="history response was unavailable",
                        )
                    else:
                        return self._public_conversation_page(typed_page)
                return self._public_conversation_page(page)
            finally:
                with self._conversation_requests_lock:
                    self._conversation_requests.pop(request_id, None)
                self._conversation_read_slots.release()

        result: list[ConversationHistoryPage] = []
        errors: list[Exception] = []
        finished = threading.Event()

        def read_inactive() -> None:
            try:
                result.append(self._inactive_conversation_page(request))
            except Exception as exc:
                errors.append(exc)
            finally:
                self._conversation_read_slots.release()
                finished.set()

        try:
            threading.Thread(
                target=read_inactive,
                name="codex-flow-inactive-conversation-read",
                daemon=True,
            ).start()
        except RuntimeError:
            self._conversation_read_slots.release()
            raise
        if not finished.wait(CONVERSATION_READ_DEADLINE_SECONDS):
            return self._public_conversation_page(
                ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.UNAVAILABLE,
                    None,
                    reason="conversation read exceeded its deadline",
                )
            )
        if errors:
            if isinstance(errors[0], IpcError):
                raise errors[0]
            return self._public_conversation_page(
                ConversationHistoryPage(
                    request.subject_kind,
                    request.subject_id,
                    request.thread_id,
                    ConversationHistoryStatus.UNAVAILABLE,
                    None,
                    reason="conversation read failed",
                )
            )
        return self._public_conversation_page(result[0])

    def _create_control(self, payload: dict[str, object], kind: ControlCommandKind) -> dict[str, object]:
        required = {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "thread_id",
            "turn_id",
            "command_id",
            "payload",
        }
        if set(payload) != required:
            raise IpcError("live control request has an unsupported shape")
        dispatch_id, generation, attempt, thread_id, turn_id, command_id = self._control_identity(payload)
        if not command_id:
            raise IpcError("live control command id is required")
        raw_payload = payload.get("payload")
        if kind is ControlCommandKind.STEER:
            if not isinstance(raw_payload, str) or not raw_payload.strip() or len(raw_payload.encode("utf-8")) > 8192:
                raise IpcError("steer payload exceeds its byte limit")
        elif raw_payload is not None:
            raise IpcError("interrupt payload must be null")
        active = getattr(self, "_active_turns", {}).get(dispatch_id)
        if active is None:
            raise IpcError("live control turn is not currently bound")
        if active != (generation, attempt, thread_id, turn_id):
            raise IpcError("live control identity is stale")
        command = self.ledger.create_control_command(
            dispatch_id,
            command_id=command_id,
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=kind,
            payload=raw_payload if isinstance(raw_payload, str) else None,
        )
        return {"version": 1, "ok": True, "command": _public_control_command(command)}

    def _worker_capability(self, payload: dict[str, object]) -> tuple[dict[str, object], int, int, str]:
        row = self._queue(payload)
        generation, attempt = self._strict_worker_process_identity(payload, row)
        token = payload.get("token")
        if not isinstance(token, str):
            raise IpcError("worker control capability is stale")
        try:
            self.ledger.worker_attempt_capability(
                str(row["dispatch_id"]),
                generation=generation,
                attempt=attempt,
                token=token,
            )
        except (LedgerError, ValueError) as exc:
            raise IpcError("worker control capability is invalid") from exc
        return row, generation, attempt, token

    def _renew_worker_liveness(
        self, row: dict[str, object], *, generation: int, attempt: int, token: str
    ) -> dict[str, object]:
        try:
            return self.ledger.renew_worker_liveness(
                str(row["dispatch_id"]),
                generation=generation,
                attempt=attempt,
                token=token,
                lease_seconds=self.lease_seconds,
                epoch=int(self.epoch or 0),
            )
        except (LedgerError, ValueError) as exc:
            raise IpcError("worker control capability is invalid") from exc

    def _bind_turn(self, payload: dict[str, object]) -> dict[str, object]:
        expected = {"version", "operation", "dispatch_id", "generation", "attempt", "token", "thread_id", "turn_id"}
        if set(payload) != expected:
            raise IpcError("turn binding request has an unsupported shape")
        row, generation, attempt, _token = self._worker_capability(payload)
        generation, attempt, thread_id, turn_id = self._strict_worker_turn_identity(payload, row)
        active_turns = getattr(self, "_active_turns", None)
        if active_turns is None:
            active_turns = {}
            self._active_turns = active_turns
        previous = active_turns.get(str(row["dispatch_id"]))
        if previous is not None and previous != (generation, attempt, thread_id, turn_id):
            self._close_live_subject(str(row["dispatch_id"]), previous)
        active_turns[str(row["dispatch_id"])] = (
            generation,
            attempt,
            thread_id,
            turn_id,
        )
        return {"version": 1, "ok": True, "dispatch_id": row["dispatch_id"], "turn_id": turn_id}

    def _worker_event(self, payload: dict[str, object]) -> dict[str, object]:
        required = {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "token",
            "thread_id",
            "turn_id",
            "sequence",
            "kind",
            "text",
        }
        if set(payload) - (required | {"payload_sha256"}) or not required.issubset(payload):
            raise IpcError("worker event request has an unsupported shape")
        row, generation, attempt, token = self._worker_capability(payload)
        generation, attempt, thread_id, turn_id = self._strict_worker_turn_identity(payload, row)
        active = getattr(self, "_active_turns", {}).get(str(row["dispatch_id"]))
        if active != (generation, attempt, thread_id, turn_id):
            raise IpcError("worker event turn identity is stale")
        sequence = payload["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise IpcError("worker event sequence is invalid")
        kind = payload["kind"]
        text = payload["text"]
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise IpcError("worker event kind is invalid")
        if text is not None and not isinstance(text, str):
            raise IpcError("worker event text is invalid")
        if text is not None:
            try:
                if len(text.encode("utf-8")) > 8192 or "\x00" in text:
                    raise IpcError("worker event text exceeds its byte limit")
            except UnicodeEncodeError as exc:
                raise IpcError("worker event text is invalid") from exc
        payload_sha256 = payload.get("payload_sha256")
        if payload_sha256 is not None:
            if not isinstance(payload_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
                raise IpcError("worker event payload digest is invalid")
            if (
                text is None
                or hashlib.sha256(redact_diagnostic_text(text).encode("utf-8")).hexdigest() != payload_sha256
            ):
                raise IpcError("worker event payload digest conflicts with text")
        if is_lossy_worker_diagnostic_method(kind):
            # Progress is useful to an interactive observer but is not
            # lifecycle authority.  Acknowledge it after identity validation
            # without renewing or writing the SQLite ledger.  Heartbeat owns
            # the lease and submit_result owns terminal ingress, so a noisy
            # model stream cannot serialize either boundary behind a ring
            # transaction.
            return {"version": 1, "ok": True, "event": None, "evicted": True}
        try:
            terminal_integrity = (
                self._capture_control_repair_terminal_integrity(row) if kind == "turn/completed" else None
            )
            event = self.ledger.append_worker_diagnostic(
                str(row["dispatch_id"]),
                generation=generation,
                attempt=attempt,
                token=token,
                epoch=int(self.epoch or 0),
                lease_seconds=self.lease_seconds,
                kind=kind,
                text=text,
                payload_sha256=payload_sha256,
                sequence=sequence,
                terminal_integrity=terminal_integrity,
            )
        except (LedgerError, ValueError, WorktreeError) as exc:
            raise IpcError("worker event capability or sequence is invalid") from exc
        response: dict[str, object] = {"version": 1, "ok": True, "event": event}
        if event is None:
            response["evicted"] = True
        return response

    def _poll_commands(self, payload: dict[str, object]) -> dict[str, object]:
        expected = {"version", "operation", "dispatch_id", "generation", "attempt", "token", "thread_id", "turn_id"}
        if set(payload) != expected:
            raise IpcError("command poll request has an unsupported shape")
        row, generation, attempt, token = self._worker_capability(payload)
        generation, attempt, thread_id, turn_id = self._strict_worker_turn_identity(payload, row)
        self._renew_worker_liveness(row, generation=generation, attempt=attempt, token=token)
        # A harness restart clears only in-memory active-turn state.  The
        # worker's first poll rebinds the exact live turn under the same
        # durable attempt capability; no replacement turn can be addressed.
        active_turns = getattr(self, "_active_turns", None)
        if active_turns is None:
            active_turns = {}
            self._active_turns = active_turns
        active_turns[str(row["dispatch_id"])] = (
            generation,
            attempt,
            thread_id,
            turn_id,
        )
        commands = self.ledger.claim_control_commands(
            str(row["dispatch_id"]),
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        with self._conversation_requests_lock:
            history_requests = []
            for request_id, pending in self._conversation_requests.items():
                if (
                    pending.get("dispatch_id") == str(row["dispatch_id"])
                    and pending.get("generation") == generation
                    and pending.get("attempt") == attempt
                    and pending.get("thread_id") == thread_id
                    and pending.get("turn_id") == turn_id
                ):
                    request = pending.get("request")
                    if isinstance(request, ConversationHistoryRequest):
                        history_requests.append({"request_id": request_id, **request.to_json()})
        return {"version": 1, "ok": True, "commands": list(commands), "history_requests": history_requests}

    def _ack_control(self, payload: dict[str, object]) -> dict[str, object]:
        expected = {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "token",
            "command_id",
            "thread_id",
            "turn_id",
            "kind",
            "state",
            "detail",
        }
        if set(payload) != expected:
            raise IpcError("control acknowledgement has an unsupported shape")
        row, capability_generation, capability_attempt, token = self._worker_capability(payload)
        _, generation, attempt, thread_id, turn_id, command_id = self._control_identity(payload)
        if (generation, attempt) != (capability_generation, capability_attempt):
            raise IpcError("control acknowledgement process identity is stale")
        if str(row["dispatch_id"]) != str(payload["dispatch_id"]):
            raise IpcError("control acknowledgement dispatch is stale")
        if payload["kind"] not in {kind.value for kind in ControlCommandKind}:
            raise IpcError("control acknowledgement kind is invalid")
        try:
            state = ControlCommandState(str(payload["state"]))
        except ValueError as exc:
            raise IpcError("control acknowledgement state is invalid") from exc
        if state not in {
            ControlCommandState.ACKNOWLEDGED,
            ControlCommandState.REJECTED,
            ControlCommandState.UNRESOLVED,
        }:
            raise IpcError("control acknowledgement state is invalid")
        command = self.ledger.control_command(command_id)
        if (
            int(command["generation"]),
            int(command["attempt"]),
            str(command["thread_id"]),
            str(command["turn_id"]),
            str(command["kind"]),
        ) != (generation, attempt, thread_id, turn_id, str(payload["kind"])):
            raise IpcError("control acknowledgement identity is stale")
        detail = payload.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise IpcError("control acknowledgement detail is invalid")
        if detail is not None:
            try:
                if len(detail.encode("utf-8")) > 256 or "\x00" in detail:
                    raise IpcError("control acknowledgement detail exceeds its byte limit")
                detail = redact_diagnostic_text(detail, limit=256)
            except (UnicodeEncodeError, ValueError) as exc:
                raise IpcError("control acknowledgement detail is invalid") from exc
        if command.get("state") != ControlCommandState.SENT.value:
            raise IpcError("control acknowledgement is not bound to a sent command")
        self._renew_worker_liveness(row, generation=generation, attempt=attempt, token=token)
        acknowledged = self.ledger.acknowledge_control_command(
            command_id,
            state=state,
            acknowledgement={"detail": detail},
        )
        return {"version": 1, "ok": True, "command": acknowledged}

    def _retry(self, payload: dict[str, object]) -> dict[str, object]:
        normalized = dict(payload)
        normalized["operation"] = "recovery_action"
        normalized["action_kind"] = RecoveryActionKind.RETRY.value
        return self._recovery_action(normalized)

    def _recovery_action(self, payload: dict[str, object]) -> dict[str, object]:
        required = {"version", "operation", "dispatch_id", "action_id", "expected_revision", "action_kind", "reason"}
        if set(payload) - (required | {"requested_budget", "compatibility_rebind"}) or not required.issubset(payload):
            raise IpcError("recovery action request has an unsupported shape")
        dispatch_id = payload["dispatch_id"]
        action_id = payload["action_id"]
        expected_revision = payload["expected_revision"]
        reason = payload["reason"]
        if not isinstance(dispatch_id, str) or not isinstance(action_id, str) or not isinstance(reason, str):
            raise IpcError("recovery action identity is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise IpcError("recovery action revision is invalid")
        try:
            kind = RecoveryActionKind(str(payload["action_kind"]))
        except ValueError as exc:
            raise IpcError("recovery action kind is invalid") from exc
        requested: RetryBudgetChange | None = None
        rebind: CompatibilityRebind | None = None
        if "requested_budget" in payload:
            raw_budget = payload["requested_budget"]
            if not isinstance(raw_budget, dict):
                raise IpcError("recovery action budget is invalid")
            try:
                if set(raw_budget) not in {
                    frozenset(
                        {
                            "pre_identity_budget",
                            "invalid_chain_budget",
                            "schema_envelope_budget",
                            "post_identity_loss_budget",
                        }
                    ),
                    frozenset(
                        {
                            "pre_identity_budget",
                            "invalid_chain_budget",
                            "schema_envelope_budget",
                            "post_identity_loss_budget",
                            "provider_transient_budget",
                        }
                    ),
                }:
                    raise ValueError("budget shape is invalid")
                requested = RetryBudgetChange(**raw_budget)
            except (TypeError, ValueError) as exc:
                raise IpcError("recovery action budget is invalid") from exc
        if "compatibility_rebind" in payload:
            raw_rebind = payload["compatibility_rebind"]
            try:
                if not isinstance(raw_rebind, dict) or set(raw_rebind) != {
                    "expected_compatibility_sha256",
                    "proposed_compatibility_sha256",
                    "expected_profile_sha256",
                    "proposed_profile_sha256",
                    "expected_generation",
                    "expected_attempt",
                }:
                    raise ValueError("compatibility rebind shape is invalid")
                rebind = CompatibilityRebind(**raw_rebind)
            except (TypeError, ValueError) as exc:
                raise IpcError("compatibility rebind facts are invalid") from exc
        if kind is RecoveryActionKind.COMPATIBILITY_REBIND:
            if rebind is None or requested is not None:
                raise IpcError("compatibility rebind request has conflicting facts")
            queue = self.ledger.queue_dispatch(dispatch_id)
            try:
                route = strict_json_loads(str(queue["route_json"]), max_bytes=16_384)
                capsule = strict_json_loads(str(queue["capsule_json"]), max_bytes=2_000_000)
                if not isinstance(route, dict) or not isinstance(capsule, dict):
                    raise ValueError("queued authority is not an object")
                raw_requirements = capsule.get("plugin_requirements", [])
                route_requirements = route.get("plugin_requirements", [])
                raw_snapshots = route.get("plugin_capabilities", [])
                if raw_requirements != route_requirements or not isinstance(raw_requirements, list):
                    raise ValueError("queued plugin requirements changed")
                if not isinstance(raw_snapshots, list):
                    raise ValueError("queued plugin snapshots are malformed")
                requirements = tuple(PluginRequirement.from_json(item) for item in raw_requirements)
                snapshots: list[PluginCapabilitySnapshot] = []
                for raw_snapshot in raw_snapshots:
                    if not isinstance(raw_snapshot, dict) or not isinstance(raw_snapshot.get("capability_sha256"), str):
                        raise ValueError("queued plugin snapshot is malformed")
                    snapshot = PluginCapabilitySnapshot.from_json(
                        {key: value for key, value in raw_snapshot.items() if key != "capability_sha256"}
                    )
                    if raw_snapshot["capability_sha256"] != snapshot.capability_digest:
                        raise ValueError("queued plugin snapshot digest changed")
                    snapshots.append(snapshot)
                if len(requirements) != len(snapshots):
                    raise ValueError("queued plugin snapshot cardinality changed")
                profile_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve(strict=True)
                with _verified_skill_input(requirements, tuple(snapshots), profile_home):
                    profile = NativeProfileProjection.load(profile_home)
                    profile.verify_worker_sources()
                    if (
                        profile.profile_sha256 != rebind.proposed_profile_sha256
                        or profile.worker_compatibility_sha256 != rebind.proposed_compatibility_sha256
                    ):
                        raise ValueError("proposed identity tuple is not the verified installed runtime")
            except (OSError, PluginCapabilityError, TypeError, ValueError, RuntimeError) as exc:
                raise IpcError("installed runtime compatibility could not be verified") from exc
            action = self.ledger.apply_recovery_action(
                dispatch_id,
                action_id=action_id,
                expected_revision=expected_revision,
                action_kind=kind,
                reason=reason,
                compatibility_rebind=rebind,
            )
        else:
            if rebind is not None:
                raise IpcError("non-rebind recovery action carries compatibility facts")
            action = self.ledger.apply_recovery_action(
                dispatch_id,
                action_id=action_id,
                expected_revision=expected_revision,
                action_kind=kind,
                reason=reason,
                requested_budget=requested,
            )
        if kind is RecoveryActionKind.CANCEL:
            self._advance_worker_cancellation(action)
        return {"version": 1, "ok": True, "action": action}

    def _queue(self, payload: dict[str, object]) -> dict[str, object]:
        dispatch_id = payload.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            raise IpcError("dispatch id is required")
        if self.epoch is None:
            raise IpcError("harness lease is not acquired")
        row = self.ledger.queue_dispatch(dispatch_id)
        if row.get("claim_epoch") != self.epoch:
            raise IpcError("queue claim epoch is stale")
        return row

    def _bind_worker(self, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) != {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "token",
            "thread_id",
        }:
            raise IpcError("worker identity request has an unsupported shape")
        row, generation, attempt, token = self._worker_capability(payload)
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            raise IpcError("worker identity binding is incomplete")
        try:
            ThreadIdentity(thread_id)
        except ValueError as exc:
            raise IpcError("worker thread identity is invalid") from exc
        try:
            bound = self.ledger.bind_worker_thread(
                str(row["dispatch_id"]),
                epoch=int(self.epoch),
                generation=generation,
                attempt=attempt,
                token=token,
                thread_id=thread_id,
            )
        except (LedgerError, ValueError) as exc:
            raise IpcError("worker capability does not match queue attempt") from exc
        latest = (
            self.ledger._db()
            .execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM diagnostic_ring WHERE dispatch_id = ?",
                (str(row["dispatch_id"]),),
            )
            .fetchone()
        )
        return {
            "version": 1,
            "ok": True,
            "dispatch_id": row["dispatch_id"],
            "thread_id": bound["thread_id"],
            "next_event_sequence": int(latest[0]) if latest is not None else 0,
        }

    def _heartbeat(self, payload: dict[str, object]) -> dict[str, object]:
        if set(payload) != {
            "version",
            "operation",
            "dispatch_id",
            "generation",
            "attempt",
            "token",
        }:
            raise IpcError("worker heartbeat request has an unsupported shape")
        row, generation, attempt, token = self._worker_capability(payload)
        live = self._renew_worker_liveness(row, generation=generation, attempt=attempt, token=token)
        return {
            "version": 1,
            "ok": True,
            "operation": "heartbeat",
            "dispatch_id": row["dispatch_id"],
            "last_seen_at": live["last_seen_at"],
        }

    def _submit_result(self, payload: dict[str, object]) -> dict[str, object]:
        required = {
            "dispatch_id",
            "generation",
            "attempt",
            "backend",
            "workspace_path",
            "schema_sha256",
            "token",
            "raw_result",
        }
        if set(payload) - ({"version", "operation"} | required) or not required.issubset(payload):
            raise IpcError("worker result request has an unsupported shape")
        try:
            row, generation, attempt, token = self._worker_capability(payload)
        except IpcError as exc:
            dispatch_id = payload.get("dispatch_id")
            if isinstance(dispatch_id, str):
                code = WorkerResultRejectionCode.CAPABILITY_STALE
                self._record_worker_result_rejection(dispatch_id, code)
                raise WorkerResultRejected(code) from exc
            raise
        if (
            payload["backend"] != row["backend"]
            or payload["workspace_path"] != row["workspace_path"]
            or payload["schema_sha256"] != row["result_contract_sha256"]
        ):
            code = WorkerResultRejectionCode.CAPABILITY_STALE
            self._record_worker_result_rejection(str(row["dispatch_id"]), code)
            raise WorkerResultRejected(code)
        if not isinstance(payload["raw_result"], str):
            raise IpcError("worker raw result must be UTF-8 text")
        raw = payload["raw_result"].encode("utf-8")
        if len(raw) > 65_536:
            raise IpcError("worker raw result exceeds bounded limit")
        try:
            self._assert_control_repair_preingestion_authority(row)
            terminal = self.ledger.commit_queue_result(
                str(row["dispatch_id"]),
                generation=generation,
                attempt=attempt,
                token=token,
                raw_result=raw,
            )
        except StaleWriter as exc:
            code = (
                WorkerResultRejectionCode.CAPABILITY_EXPIRED
                if "capability has expired" in str(exc)
                else WorkerResultRejectionCode.CAPABILITY_STALE
            )
            self._record_worker_result_rejection(str(row["dispatch_id"]), code)
            raise WorkerResultRejected(code) from exc
        except (TypeError, ValueError, WorktreeError):
            # The worker request contains only bounded UTF-8 text.  Keep the
            # parser's detailed failure private and expose one stable reason
            # that cannot leak a token or transcript fragment.
            code = WorkerResultRejectionCode.MALFORMED_OUTPUT
            self._record_worker_result_rejection(str(row["dispatch_id"]), code)
            raise WorkerResultRejected(code) from None
        active = getattr(self, "_active_turns", {}).get(str(row["dispatch_id"]))
        if active is not None:
            self._close_live_subject(str(row["dispatch_id"]), active)
        self._record_program_queue_result(terminal)
        if active is not None:
            _generation, _attempt, _thread_id, active_turn_id = active
            for command in self.ledger.control_commands(str(row["dispatch_id"])):
                if (
                    command.get("state") == ControlCommandState.SENT.value
                    and command.get("kind") == ControlCommandKind.INTERRUPT.value
                    and command.get("turn_id") == active_turn_id
                ):
                    try:
                        terminal_status = terminal.get("terminal_status")
                        acknowledgement_state = (
                            ControlCommandState.ACKNOWLEDGED
                            if terminal_status == "interrupted"
                            else ControlCommandState.REJECTED
                        )
                        self.ledger.acknowledge_control_command(
                            str(command["command_id"]),
                            state=acknowledgement_state,
                            acknowledgement={
                                "terminal_status": terminal_status,
                                "detail": (
                                    None
                                    if acknowledgement_state is ControlCommandState.ACKNOWLEDGED
                                    else "SDK turn completed without interrupt terminal evidence"
                                ),
                            },
                        )
                    except LedgerError:
                        pass
            # Any command that was never observed by the worker, or whose
            # acknowledgement was lost as the terminal result crossed the
            # IPC boundary, is unresolved rather than replayed against a
            # replacement turn.
            self.ledger.unresolved_control_commands(str(row["dispatch_id"]))
        active_turns = getattr(self, "_active_turns", None)
        if active_turns is not None:
            active_turns.pop(str(row["dispatch_id"]), None)
        digest = str(terminal.get("raw_result_sha256") or hashlib.sha256(raw).hexdigest())
        return {
            "version": 1,
            "ok": True,
            "dispatch_id": row["dispatch_id"],
            "terminal_status": terminal["state"],
            "result_sha256": digest,
        }

    def _record_worker_result_rejection(self, dispatch_id: str, code: WorkerResultRejectionCode) -> None:
        """Retain one sanitized diagnostic for a rejected worker result."""

        try:
            self.ledger.append_diagnostic(
                dispatch_id,
                kind="worker_result_rejected",
                text=code.value,
            )
        except LedgerError:
            # The rejection itself remains fail-closed even if diagnostic
            # retention loses a race with terminalization or cancellation.
            pass

    def _control_capsule(self, run_id: str, milestone_id: str) -> ExecutionCapsule:
        """Load the already durable ordinary-control capsule."""

        from .controller import Controller

        controller = Controller(self.state_root)
        try:
            return controller._load_durable_capsule(controller.status(run_id, milestone_id))
        finally:
            controller.close()

    def _enqueue_control_dispatch(
        self,
        capsule: ExecutionCapsule,
        *,
        role: str,
        generation: int,
        candidate_sha: str | None = None,
        finding_ids: Sequence[str] = (),
    ) -> None:
        """Use the existing authenticated Controller queue boundary once."""

        from .controller import Controller

        dispatch_id = str(DispatchId.from_parts(capsule.run_id, capsule.milestone_id, role, generation))
        try:
            existing = self.ledger.queue_dispatch(dispatch_id)
        except RecordNotFound:
            existing = None
        else:
            self._validate_control_dispatch_reuse(
                existing,
                capsule=capsule,
                role=role,
                generation=generation,
                candidate_sha=candidate_sha,
                finding_ids=finding_ids,
            )
            return
        if role == "executor":
            action_context = {
                "candidate_sha": candidate_sha,
                "finding_ids": list(finding_ids),
                "repair_generation": generation,
            }
        else:
            action_context = {
                "candidate_sha": candidate_sha,
                "review_role": role,
                "generation": generation,
            }
        controller = Controller(self.state_root, worktrees=self._worktrees)
        try:
            controller.enqueue(
                capsule,
                role=role,
                generation=generation,
                backend="sdk_headless",
                action_json=json.dumps(action_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                plan_path="internal",
            )
        finally:
            controller.close()

    def _validate_control_dispatch_reuse(
        self,
        row: Mapping[str, object],
        *,
        capsule: ExecutionCapsule,
        role: str,
        generation: int,
        candidate_sha: str | None,
        finding_ids: Sequence[str],
    ) -> None:
        """Validate every immutable binding before reusing an existing queue row."""

        dispatch_id = str(DispatchId.from_parts(capsule.run_id, capsule.milestone_id, role, generation))
        if (
            row.get("dispatch_id") != dispatch_id
            or row.get("run_id") != str(capsule.run_id)
            or row.get("milestone_id") != str(capsule.milestone_id)
            or row.get("role") != role
            or row.get("generation") != generation
            or row.get("backend") != "sdk_headless"
            or row.get("workspace_path") != os.fspath(capsule.workspace_path.resolve())
        ):
            raise WorktreeError("existing control dispatch has conflicting immutable identity")
        context_raw = row.get("action_json")
        if not isinstance(context_raw, str):
            raise WorktreeError("existing control dispatch lacks its immutable action context")
        try:
            context = strict_json_loads(context_raw, max_bytes=16_384)
        except ValueError as exc:
            raise WorktreeError("existing control dispatch action context is malformed") from exc
        if role == "executor":
            expected: dict[str, object] = {
                "candidate_sha": candidate_sha,
                "finding_ids": list(finding_ids),
                "repair_generation": generation,
            }
        else:
            expected = {
                "candidate_sha": candidate_sha,
                "review_role": role,
                "generation": generation,
            }
        if context != expected:
            raise WorktreeError("existing control dispatch action context conflicts with its requested binding")

    @staticmethod
    def _control_authority_roles(capsule: ExecutionCapsule) -> tuple[str, ...]:
        """Return the declared review roles in deterministic mode order."""

        role_by_mode = {
            AcceptanceMode.OBJECTIVE: "code-reviewer",
            AcceptanceMode.VISUAL: "visual-reviewer",
            AcceptanceMode.ARCHITECTURE: "architecture-reviewer",
        }
        return tuple(role_by_mode[mode] for mode in capsule.acceptance_modes)

    def _queue_control_reviews(self, capsule: ExecutionCapsule, candidate_sha: str, *, generation: int = 1) -> None:
        """Queue each declared exact-candidate authority exactly once."""

        config = load_workflow_config(self.state_root / "workflow.toml")
        config.derive_authorities(capsule.acceptance_modes)
        for role in self._control_authority_roles(capsule):
            self._enqueue_control_dispatch(
                capsule,
                role=role,
                generation=generation,
                candidate_sha=candidate_sha,
            )

    @staticmethod
    def _control_candidate_blocker(facts: Sequence[object], candidate_sha: str) -> TypedBlocker | None:
        for item in reversed(facts):
            if getattr(item, "kind", None) != "candidate_recorded":
                continue
            data = getattr(item, "data", {})
            if not isinstance(data, Mapping) or data.get("candidate_sha") != candidate_sha:
                continue
            raw = data.get("blocker")
            if raw is None:
                return None
            if not isinstance(raw, Mapping):
                raise WorktreeError("control candidate blocker is not an object")
            try:
                return TypedBlocker.from_json(raw)
            except (TypeError, ValueError) as exc:
                raise WorktreeError("control candidate blocker is malformed") from exc
        return None

    @staticmethod
    def _control_blocking_findings(facts: Sequence[object], candidate_sha: str, generation: int) -> tuple[str, ...]:
        finding_ids: list[str] = []
        for item in facts:
            if getattr(item, "kind", None) != "review_completed":
                continue
            data = getattr(item, "data", {})
            if not isinstance(data, Mapping) or data.get("candidate_sha") != candidate_sha:
                continue
            if data.get("generation") != generation or data.get("promotion_blocking") is not True:
                continue
            review = data.get("review")
            raw_findings = review.get("findings", []) if isinstance(review, Mapping) else []
            if not isinstance(raw_findings, list | tuple):
                continue
            for finding in raw_findings:
                if not isinstance(finding, Mapping):
                    continue
                if finding.get("promotion_blocking") is True and finding.get("severity") in {"P0", "P1"}:
                    finding_id = finding.get("finding_id")
                    if isinstance(finding_id, str) and finding_id not in finding_ids:
                        finding_ids.append(finding_id)
        return tuple(finding_ids)

    def _control_result_issue(self, row: Mapping[str, object], exc: BaseException) -> None:
        """Close an unprocessable control result with an accurate blocker."""

        run_id = row.get("run_id")
        milestone_id = row.get("milestone_id")
        if not isinstance(run_id, str) or not isinstance(milestone_id, str):
            return
        try:
            state = self.ledger.current_state(run_id, milestone_id)
            if state in {WorkflowState.ACCEPTED, WorkflowState.FAILED, WorkflowState.BLOCKED, WorkflowState.CANCELLED}:
                return
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.BLOCKED,
                expected_state=state,
                phase=LifecyclePhase.RECOVERY,
                kind="control_result_rejected",
                data={
                    "dispatch_id": str(row.get("dispatch_id")),
                    "role": str(row.get("role")),
                    "reason": type(exc).__name__,
                },
                reason=ReasonCode.ENVIRONMENT_BLOCKED,
            )
        except (LedgerError, ValueError):
            pass

    def _advance_control_acceptance(self, capsule: ExecutionCapsule, candidate_sha: str, generation: int) -> None:
        """Advance only from complete exact-candidate authority evidence."""

        run_id = str(capsule.run_id)
        milestone_id = str(capsule.milestone_id)
        if generation == 2:
            repair_dispatch_id = str(DispatchId.from_parts(run_id, milestone_id, "executor", 2))
            repair_row = self.ledger.queue_dispatch(repair_dispatch_id)
            repair_result_sha256 = repair_row.get("raw_result_sha256")
            if not isinstance(repair_result_sha256, str):
                raise WorktreeError("control repair queue lacks its accepted result digest")
            self._assert_control_repair_terminal_authority(
                capsule,
                dispatch_id=repair_dispatch_id,
                candidate_sha=candidate_sha,
                result_sha256=repair_result_sha256,
            )
        facts = self.ledger.review_lifecycle(run_id, milestone_id)
        promotion = next(
            (
                item
                for item in facts
                if item.kind == "promotion_accepted" and item.data.get("candidate_sha") == candidate_sha
            ),
            None,
        )
        if promotion is not None:
            if self.ledger.current_state(run_id, milestone_id) is WorkflowState.REVIEWING:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.ACCEPTED,
                    expected_state=WorkflowState.REVIEWING,
                    phase=LifecyclePhase.ACCEPTANCE,
                    kind="promotion_recovered",
                    data=dict(promotion.data),
                    reason=ReasonCode.TERMINAL_OUTCOME,
                )
            return
        if any(
            item.kind in {"re_review_rejected", "control_result_rejected"}
            and item.data.get("candidate_sha") == candidate_sha
            for item in facts
        ):
            return
        roles = set(self._control_authority_roles(capsule))
        completed_roles = {
            str(item.data.get("reviewer_role"))
            for item in facts
            if item.kind == "review_completed"
            and item.data.get("candidate_sha") == candidate_sha
            and item.data.get("generation") == generation
        }
        if not roles.issubset(completed_roles):
            return
        reviews = [
            item
            for item in facts
            if item.kind == "review_completed"
            and item.data.get("candidate_sha") == candidate_sha
            and item.data.get("generation") == generation
            and item.data.get("reviewer_role") in roles
        ]
        blocking = any(item.data.get("promotion_blocking") is True for item in reviews)
        blocker = self._control_candidate_blocker(facts, candidate_sha)
        if not blocking and blocker is None:
            review_ids = sorted(str(item.data["review_id"]) for item in reviews)
            self.ledger.record_review_transition(
                run_id,
                milestone_id,
                WorkflowState.ACCEPTED,
                expected_state=WorkflowState.REVIEWING,
                phase=LifecyclePhase.ACCEPTANCE,
                kind="promotion_accepted",
                data={"candidate_sha": candidate_sha, "generation": generation, "review_ids": review_ids},
                reason=ReasonCode.TERMINAL_OUTCOME,
            )
            return

        eligible_findings = self._control_blocking_findings(facts, candidate_sha, generation)
        repair = next(
            (
                item
                for item in facts
                if item.kind == "repair_requested" and item.data.get("candidate_sha") == candidate_sha
            ),
            None,
        )
        if generation == 1 and eligible_findings:
            if repair is None:
                self.ledger.record_review_transition(
                    run_id,
                    milestone_id,
                    WorkflowState.REPAIR_REQUIRED,
                    expected_state=WorkflowState.REVIEWING,
                    phase=LifecyclePhase.REPAIR,
                    kind="repair_requested",
                    data={
                        "candidate_sha": candidate_sha,
                        "finding_ids": list(eligible_findings),
                        "same_owner": True,
                        "repair_generation": 2,
                    },
                    reason=ReasonCode.REVIEW_REJECTED,
                )
            self._enqueue_control_dispatch(
                capsule,
                role="executor",
                generation=2,
                candidate_sha=candidate_sha,
                finding_ids=eligible_findings,
            )
            return

        target = (
            WorkflowState.BLOCKED
            if blocker is not None and blocker.kind is BlockerKind.EXTERNAL
            else WorkflowState.NEEDS_DECISION
            if blocker is not None and blocker.kind is BlockerKind.DECISION
            else WorkflowState.FAILED
        )
        self.ledger.record_review_transition(
            run_id,
            milestone_id,
            target,
            expected_state=WorkflowState.REVIEWING,
            phase=LifecyclePhase.RECOVERY,
            kind="re_review_rejected" if generation > 1 else "candidate_blocked",
            data={
                "candidate_sha": candidate_sha,
                "generation": generation,
                "review_ids": sorted(str(item.data["review_id"]) for item in reviews),
                "finding_ids": list(eligible_findings),
                "blocker": blocker.to_json() if blocker is not None else None,
            },
            reason=(
                ReasonCode.ENVIRONMENT_BLOCKED
                if target is WorkflowState.BLOCKED
                else ReasonCode.DECISION_REQUIRED
                if target is WorkflowState.NEEDS_DECISION
                else ReasonCode.REVIEW_REJECTED
            ),
        )

    def _record_control_queue_result(self, row: Mapping[str, object]) -> None:
        """Translate one terminal ordinary-control queue row into acceptance facts."""

        run_id = row.get("run_id")
        milestone_id = row.get("milestone_id")
        raw = row.get("raw_result_json")
        if not isinstance(run_id, str) or not isinstance(milestone_id, str) or not isinstance(raw, str):
            return
        dispatch_id = str(row.get("dispatch_id"))
        role = str(row.get("role"))
        try:
            capsule = self._control_capsule(run_id, milestone_id)
            generation = int(row.get("generation", 0))
            if role == "executor":
                terminal_status = str(row.get("terminal_status"))
                result_sha256 = row.get("raw_result_sha256")
                if not isinstance(result_sha256, str):
                    raise WorktreeError("control executor queue lacks a result digest")
                predecessor_sha: str | None = None
                repair_findings: tuple[str, ...] = ()
                context = row.get("action_json")
                if isinstance(context, str):
                    try:
                        context_value = strict_json_loads(context, max_bytes=16_384)
                    except ValueError:
                        raise WorktreeError("control executor queue action context is malformed") from None
                    if not isinstance(context_value, dict):
                        raise WorktreeError("control executor queue action context is malformed")
                    if context_value:
                        if set(context_value) != {"candidate_sha", "finding_ids", "repair_generation"}:
                            raise WorktreeError("control executor queue action context has an unsupported shape")
                        raw_candidate = context_value.get("candidate_sha")
                        raw_findings = context_value.get("finding_ids")
                        if (
                            not isinstance(raw_candidate, str)
                            or not isinstance(raw_findings, list)
                            or any(not isinstance(item, str) for item in raw_findings)
                            or context_value.get("repair_generation") != generation
                        ):
                            raise WorktreeError("control executor queue action context is not bound to its dispatch")
                        predecessor_sha = raw_candidate
                        repair_findings = tuple(raw_findings)
                        repair_facts = self.ledger.review_lifecycle(run_id, milestone_id)
                        expected_repair = next(
                            (
                                fact.data.get("finding_ids")
                                for fact in repair_facts
                                if fact.kind == "repair_requested" and fact.data.get("candidate_sha") == predecessor_sha
                            ),
                            None,
                        )
                        if not isinstance(expected_repair, list | tuple) or tuple(expected_repair) != repair_findings:
                            raise WorktreeError("control executor queue findings conflict with its repair authority")
                if terminal_status == "completed" and generation == 1:
                    # A completed direct controller execution already owns a
                    # terminal HEAD/workspace/Git/protected snapshot.  Queue
                    # result replay must bind to that immutable snapshot
                    # rather than trusting a later live checkout inspection.
                    try:
                        retained = self.ledger.get_execution_integrity(run_id, milestone_id)
                    except KeyError:
                        retained = None
                    retained_sha = retained.workspace_terminal_head_sha if retained is not None else None
                    try:
                        execution = self.ledger.get_execution(run_id, milestone_id)
                    except (RecordNotFound, KeyError):
                        execution = None
                    if execution is not None and execution.status is ExecutionStatus.COMPLETED:
                        if retained_sha is None:
                            raise WorktreeError("completed control execution lacks durable terminal candidate identity")
                        self._assert_control_terminal_authority(
                            capsule,
                            candidate_sha=retained_sha,
                            require_terminal_facts=True,
                        )
                        if predecessor_sha is not None:
                            raise WorktreeError("initial control queue result cannot carry repair authority")
                elif terminal_status == "completed":
                    try:
                        retained = self.ledger.get_execution_integrity(run_id, milestone_id)
                    except KeyError:
                        retained = None
                    retained_sha = retained.workspace_terminal_head_sha if retained is not None else None
                    if retained_sha is None or predecessor_sha != retained_sha:
                        raise WorktreeError("control repair predecessor conflicts with terminal candidate")
                candidate = self._worktrees.inspect_terminal_workspace(capsule, predecessor_sha=predecessor_sha)
                if terminal_status == "completed" and generation > 1:
                    self._assert_control_repair_terminal_authority(
                        capsule,
                        dispatch_id=dispatch_id,
                        candidate_sha=candidate.commit_sha or "",
                        result_sha256=result_sha256,
                    )
                    if (
                        candidate.disposition.value != "verified_commit"
                        or candidate.commit_sha != candidate.workspace_head
                        or candidate.dirty
                    ):
                        raise WorktreeError("control repair did not produce one direct clean terminal successor")
                    try:
                        from .controller import protected_paths_digest

                        parent = subprocess.run(
                            ("git", "show", "-s", "--format=%P", candidate.commit_sha or ""),
                            cwd=capsule.workspace_path,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if parent.returncode != 0 or parent.stdout.strip() != predecessor_sha:
                            raise WorktreeError("control repair did not produce one direct clean terminal successor")
                        execution = self.ledger.get_execution(run_id, milestone_id)
                        retained_protected = execution.protected_after_sha256
                        current_protected = protected_paths_digest(capsule.workspace_path, capsule.protected_paths)
                    except WorktreeError:
                        raise
                    except (RecordNotFound, OSError, RuntimeError, ValueError) as exc:
                        raise WorktreeError("control repair terminal authority could not be revalidated") from exc
                    if retained_protected is None or current_protected != retained_protected:
                        raise WorktreeError("control repair changed protected terminal authority")
                result = None
                try:
                    result = ModelFacingResult.from_agent_message(raw)
                except (TypeError, ValueError):
                    pass
                self.ledger.record_control_executor_result(
                    run_id,
                    milestone_id,
                    candidate=candidate,
                    terminal_status=terminal_status,
                    dispatch_id=dispatch_id,
                    result_sha256=result_sha256,
                    blocker=result.blocker if result is not None else None,
                )
                if terminal_status == "completed" and candidate.disposition.value == "verified_commit":
                    self._queue_control_reviews(capsule, candidate.commit_sha or "", generation=generation)
            else:
                result = review_result_from_agent_message(raw)
                context_raw = row.get("action_json")
                if not isinstance(context_raw, str):
                    raise WorktreeError("control review queue lacks candidate context")
                context = strict_json_loads(context_raw, max_bytes=16_384)
                expected_context = {
                    "candidate_sha": result.reviewed_revision,
                    "review_role": str(result.reviewer_role),
                    "generation": generation,
                }
                if context != expected_context:
                    raise WorktreeError("control review result is not bound to its queue context")
                if generation == 2:
                    repair_dispatch_id = str(DispatchId.from_parts(run_id, milestone_id, "executor", 2))
                    repair_row = self.ledger.queue_dispatch(repair_dispatch_id)
                    repair_result_sha256 = repair_row.get("raw_result_sha256")
                    if not isinstance(repair_result_sha256, str):
                        raise WorktreeError("control repair queue lacks its accepted result digest")
                    self._assert_control_repair_terminal_authority(
                        capsule,
                        dispatch_id=repair_dispatch_id,
                        candidate_sha=result.reviewed_revision,
                        result_sha256=repair_result_sha256,
                    )
                self.ledger.record_control_review(
                    run_id,
                    milestone_id,
                    candidate_sha=result.reviewed_revision,
                    dispatch_id=dispatch_id,
                    generation=generation,
                    result=result,
                )
                self._advance_control_acceptance(capsule, result.reviewed_revision, generation)
        except (AuthorityUnavailable, WorkflowConfigError, LedgerError, TypeError, ValueError, WorktreeError) as exc:
            self._control_result_issue(row, exc)

    def _record_program_queue_result(self, row: Mapping[str, object]) -> None:
        """Translate one already-terminal queue row into one program event."""

        program_id = row.get("run_id")
        milestone_id = row.get("milestone_id")
        raw = row.get("raw_result_json")
        if not isinstance(program_id, str) or not isinstance(milestone_id, str) or not isinstance(raw, str):
            return
        try:
            self.ledger.program_graph(program_id)
        except RecordNotFound:
            # Legacy queue fixtures and pre-controller dispatches can share
            # the queue table without owning a durable execution capsule.
            # They are not ordinary-control results; do not construct a
            # Controller or turn a malformed queue row into a lifecycle fact.
            try:
                self.ledger.get_execution(program_id, milestone_id)
            except (RecordNotFound, LedgerError):
                return
            self._record_control_queue_result(row)
            return
        except LedgerError:
            return
        dispatch_id = str(row.get("dispatch_id"))
        role = str(row.get("role"))
        try:
            if role == "executor":
                terminal_status = str(row.get("terminal_status"))
                result_sha256 = row.get("raw_result_sha256")
                if not isinstance(result_sha256, str):
                    raise WorktreeError("program executor queue lacks a result digest")
                if self.ledger.program_executor_terminal_recorded(
                    program_id,
                    milestone_id,
                    dispatch_id=dispatch_id,
                    terminal_status=terminal_status,
                    result_sha256=result_sha256,
                ):
                    return
                graph = self.ledger.program_graph(program_id)
                candidate_record: CandidateRecord | None = None
                blocker: TypedBlocker | None = None
                predecessor_sha: str | None = None
                action_context = row.get("action_json")
                if isinstance(action_context, str):
                    try:
                        context_value = strict_json_loads(action_context, max_bytes=16_384)
                    except ValueError:
                        context_value = None
                    if isinstance(context_value, dict) and isinstance(context_value.get("candidate_sha"), str):
                        predecessor_sha = context_value["candidate_sha"]
                try:
                    if predecessor_sha is None:
                        candidate_record = self._worktrees.inspect_terminal_workspace(
                            graph.node(milestone_id).capsule,
                        )
                    else:
                        candidate_record = self._worktrees.inspect_terminal_workspace(
                            graph.node(milestone_id).capsule,
                            predecessor_sha=predecessor_sha,
                        )
                except CandidateIntegrityError:
                    # A committed but out-of-scope/protected candidate is a
                    # durable integrity blocker, never a verified candidate.
                    blocker = TypedBlocker(
                        gate_id="candidate-integrity",
                        kind=BlockerKind.EXECUTION,
                        scope=BlockerScope.CURRENT_PROMOTION,
                        promotion_blocking=True,
                        required_action="Inspect the retained commit range and repair or abandon unauthorized paths.",
                    )
                except (WorktreeError, ValueError):
                    # The result remains durable, but an uninspectable
                    # workspace is never promoted or converted into a guess.
                    candidate_record = None
                try:
                    result = ModelFacingResult.from_agent_message(raw)
                except (TypeError, ValueError):
                    result = None
                # A candidate-integrity failure is authored by the harness
                # from exact workspace/path validation and outranks any
                # advisory blocker emitted by the model result.  Never let
                # provider output replace or downgrade that durable fact.
                if blocker is None and result is not None and result.blocker is not None:
                    blocker = result.blocker
                candidate_sha = candidate_record.commit_sha if candidate_record is not None else None
                self.ledger.record_program_executor_result(
                    program_id,
                    milestone_id,
                    candidate_sha=candidate_sha,
                    terminal_status=terminal_status,
                    dispatch_id=dispatch_id,
                    result_sha256=result_sha256,
                    candidate_record=candidate_record,
                    blocker=blocker,
                )
            else:
                result = review_result_from_agent_message(raw)
                context_raw = row.get("action_json")
                if not isinstance(context_raw, str):
                    raise WorktreeError("program review queue lacks candidate context")
                context = strict_json_loads(context_raw, max_bytes=16_384)
                if (
                    not isinstance(context, dict)
                    or context.get("candidate_sha") != result.reviewed_revision
                    or context.get("review_role") != str(result.reviewer_role)
                ):
                    raise WorktreeError("program review result is not bound to its queue context")
                self.ledger.record_program_review(program_id, milestone_id, result)
        except (LedgerError, TypeError, ValueError, WorktreeError) as exc:
            try:
                self.ledger.record_program_attention(
                    program_id,
                    event_key=f"dispatch/{dispatch_id}/result-processing",
                    payload={
                        "dispatch_id": dispatch_id,
                        "milestone_id": milestone_id,
                        "role": role,
                        "reason": type(exc).__name__,
                    },
                )
            except LedgerError:
                pass

    def _accept_connection(self, connection: socket.socket) -> str | None:
        operation: str | None = None
        try:
            # Bound the header/body read before inspecting any peer payload so
            # a same-uid client that sends only a partial frame cannot stall
            # lease renewal, recovery, or other control clients.
            connection.settimeout(min(IPC_ACCEPTED_FRAME_TIMEOUT_SECONDS, max(self.lease_seconds / 3.0, 0.05)))
            uid = peer_uid(connection)
            if uid is not None and uid != os.getuid():
                raise IpcError("IPC peer uid does not match the controller uid")
            request = decode_frame(connection)
            raw_operation = request.get("operation")
            operation = raw_operation if isinstance(raw_operation, str) else None
            self._renew_harness_lease_if_due()
            if request.get("operation") == "conversation_history":
                threading.Thread(
                    target=self._serve_conversation_connection,
                    args=(connection, request),
                    name="codex-flow-conversation-read",
                    daemon=True,
                ).start()
                return operation
            if request.get("operation") == "live_subscribe":
                subscriber = self._live_subscription(request)
                threading.Thread(
                    target=self._serve_live_connection,
                    args=(connection, subscriber),
                    name="codex-flow-live-subscription",
                    daemon=True,
                ).start()
                return operation
            response = self._request_ack(request)
        except WorkerResultRejected as exc:
            response = {
                "version": 1,
                "ok": False,
                "error": "request_rejected",
                "reason_code": exc.code.value,
            }
        except IpcError as exc:
            if exc.reason_code in {
                "item_too_large",
                "malformed_page_token",
                "page_token_identity_mismatch",
            }:
                response = {"version": 2, "ok": False, "error": str(exc.reason_code)}
            else:
                response = {"version": 1, "ok": False, "error": "request_rejected"}
        except (OSError, LedgerError):
            response = {"version": 1, "ok": False, "error": "request_rejected"}
        try:
            self._send_response(connection, response)
        finally:
            connection.close()
        return operation

    @staticmethod
    def _send_response(connection: socket.socket, response: dict[str, object]) -> None:
        """Send one bounded response without allowing projection size to kill the owner."""

        try:
            frame = encode_frame(response)
        except IpcError as exc:
            reason = (
                IpcReasonCode.RESPONSE_NOT_JSON.value
                if exc.reason_code == IpcReasonCode.REQUEST_NOT_JSON.value
                else IpcReasonCode.RESPONSE_TOO_LARGE.value
            )
            frame = encode_frame({"version": 1, "ok": False, "error": reason})
        try:
            connection.sendall(frame)
        except OSError:
            # A rejected or disconnected peer owns only its socket.  Failure
            # to deliver the bounded response must never escape the
            # foreground harness loop.
            pass

    def _serve_conversation_connection(self, connection: socket.socket, request: dict[str, object]) -> None:
        """Resolve one history read off the foreground lifecycle loop."""

        try:
            response = self._request_ack(request)
        except WorkerResultRejected as exc:
            response = {
                "version": 1,
                "ok": False,
                "error": "request_rejected",
                "reason_code": exc.code.value,
            }
        except (IpcError, OSError, LedgerError):
            response = {"version": 1, "ok": False, "error": "request_rejected"}
        try:
            self._send_response(connection, response)
        finally:
            connection.close()

    def _spawn_one(self, row: dict[str, object], *, recovery_continuation: bool = False) -> None:
        """Prepare and launch one worker, closing failed claims safely.

        Worker launch crosses a process boundary, so descriptor creation and
        ``Popen`` cannot share the ledger transaction.  If preparation or
        binding fails, remove only files created by this invocation and move
        any still-active claim to the durable human-attention state.  This
        prevents a continuation from stranding ``claimed``/``none`` state that
        a restart could accidentally try to reuse.
        """

        created_paths: list[Path] = []
        try:
            self._prepare_and_spawn_one(
                row,
                recovery_continuation=recovery_continuation,
                created_paths=created_paths,
            )
        except BaseException as exc:
            try:
                self._close_failed_spawn(row, created_paths, failure=exc)
            except Exception as terminalization_error:
                raise FailedSpawnTerminalizationError(
                    "worker spawn failed before durable terminalization"
                ) from terminalization_error
            raise

    def _prepare_and_spawn_one(
        self,
        row: dict[str, object],
        *,
        recovery_continuation: bool,
        created_paths: list[Path],
    ) -> None:
        if self.epoch is None:
            return
        if row.get("backend") != "sdk_headless":
            # The visible App-native creation API has no proven per-task
            # collaboration/agent override.  Never silently run it as a leaf
            # SDK worker or synthesize completion while the App is absent.
            raise HarnessError("App-native queue dispatch lacks a proven leaf-worker runtime boundary")
        try:
            route = json.loads(str(row["route_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HarnessError("queue route facts are not valid JSON") from exc
        policy = route.get("leaf_worker_policy") if isinstance(route, dict) else None
        expected_policy = {
            "agents.enabled": False,
            "features.multi_agent": False,
            "config_overrides": list(LEAF_WORKER_CONFIG_OVERRIDES),
        }
        if policy != expected_policy:
            raise HarnessError("SDK-headless queue lacks the controller-bound leaf-worker policy")
        try:
            capsule_value = strict_json_loads(str(row["capsule_json"]).encode("utf-8"), max_bytes=2_000_000)
            if not isinstance(capsule_value, dict):
                raise ValueError("queue capsule is not an object")
            raw_requirements = capsule_value.get("plugin_requirements", [])
            route_requirements = route.get("plugin_requirements", []) if isinstance(route, dict) else None
            route_capabilities = route.get("plugin_capabilities", []) if isinstance(route, dict) else None
            if raw_requirements != route_requirements or not isinstance(raw_requirements, list):
                raise ValueError("queued plugin requirements do not match route authority")
            plugin_requirements = tuple(PluginRequirement.from_json(item) for item in raw_requirements)
            if not isinstance(route_capabilities, list):
                raise ValueError("queued plugin capabilities are malformed")
            plugin_snapshots: list[PluginCapabilitySnapshot] = []
            for raw_snapshot in route_capabilities:
                if not isinstance(raw_snapshot, dict) or not isinstance(raw_snapshot.get("capability_sha256"), str):
                    raise ValueError("queued plugin capability digest is malformed")
                snapshot = PluginCapabilitySnapshot.from_json(
                    {key: value for key, value in raw_snapshot.items() if key != "capability_sha256"}
                )
                if raw_snapshot["capability_sha256"] != snapshot.capability_digest:
                    raise ValueError("queued plugin capability digest does not match its snapshot")
                plugin_snapshots.append(snapshot)
            if len(plugin_snapshots) != len(plugin_requirements):
                raise ValueError("queued plugin capability cardinality changed")
        except (PluginCapabilityError, TypeError, ValueError) as exc:
            raise HarnessError("queued plugin capability authority is invalid") from exc
        effective_permission = route.get("effective_permission") if isinstance(route, dict) else None
        if not isinstance(effective_permission, dict):
            raise HarnessError("SDK-headless queue lacks effective native permission facts")
        native_profile_sha256 = route.get("native_profile_sha256")
        native_compatibility_sha256 = route.get("native_compatibility_sha256")
        if native_profile_sha256 is not None and not isinstance(native_profile_sha256, str):
            raise HarnessError("queue native profile identity is malformed")
        if native_compatibility_sha256 is not None and not isinstance(native_compatibility_sha256, str):
            raise HarnessError("queue native compatibility identity is malformed")
        profile_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        try:
            with _verified_skill_input(plugin_requirements, plugin_snapshots, profile_home):
                self._issue_and_spawn_verified_worker(
                    row,
                    capsule_value=capsule_value,
                    policy=policy,
                    effective_permission=effective_permission,
                    native_profile_sha256=native_profile_sha256,
                    native_compatibility_sha256=native_compatibility_sha256,
                    plugin_requirements=plugin_requirements,
                    plugin_snapshots=tuple(plugin_snapshots),
                    profile_home=profile_home,
                    recovery_continuation=recovery_continuation,
                    created_paths=created_paths,
                )
        except PluginCapabilityError as exc:
            raise HarnessError("queued plugin capability changed before worker launch") from exc

    def _issue_and_spawn_verified_worker(
        self,
        row: dict[str, object],
        *,
        capsule_value: dict[str, object],
        policy: object,
        effective_permission: dict[str, object],
        native_profile_sha256: str | None,
        native_compatibility_sha256: str | None,
        plugin_requirements: tuple[PluginRequirement, ...],
        plugin_snapshots: tuple[PluginCapabilitySnapshot, ...],
        profile_home: Path,
        recovery_continuation: bool,
        created_paths: list[Path],
    ) -> None:
        """Issue and launch only while the fresh plugin descriptor remains held."""

        dispatch_id = str(row["dispatch_id"])
        # Bind secretless provider identity at the service boundary.  The
        # profile loader verifies the invoking environment contains the value,
        # but only its key name and digest enter SQLite.
        try:
            profile_home = profile_home.resolve(strict=True)
            profile = NativeProfileProjection.load(profile_home)
            profile.verify_worker_sources()
            if (
                native_profile_sha256 is not None
                and native_compatibility_sha256 is not None
                and (
                    profile.profile_sha256 != native_profile_sha256
                    or profile.worker_compatibility_sha256 != native_compatibility_sha256
                )
            ):
                raise WorkerCompatibilityDrift(
                    queued_profile_sha256=native_profile_sha256,
                    current_profile_sha256=profile.profile_sha256,
                    queued_compatibility_sha256=native_compatibility_sha256,
                    current_compatibility_sha256=profile.worker_compatibility_sha256,
                )
            self.ledger.configure_recovery(
                dispatch_id,
                provider_env_key=profile.provider_env_key,
                profile_sha256=profile.profile_sha256,
            )
        except HarnessError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            # Hermetic low-level tests intentionally omit a native profile;
            # production service startup remains fail-closed at its explicit
            # credential handoff boundary.
            if native_compatibility_sha256 is not None:
                raise HarnessError("shared native Codex profile cannot authorize worker launch") from exc
        attempt_suffix = f"g{int(row['generation'])}-a{int(row['attempt'])}"
        token = os.urandom(32).hex()
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        self.ledger.issue_attempt_capability(
            dispatch_id,
            generation=int(row["generation"]),
            attempt=int(row["attempt"]),
            operation="submit_result",
            schema_sha256=str(row["result_contract_sha256"]),
            workspace_path=Path(str(row["workspace_path"])),
            backend=str(row["backend"]),
            token_sha256=token_hash,
            # Queue ``deadline`` is a checkpoint/scheduling horizon.  It is
            # intentionally not reused as result authorization: recovery may
            # launch the exact active attempt after that horizon has passed.
            expires_at=None,
            lease_seconds=self.lease_seconds,
        )
        dispatch_digest = hashlib.sha256(dispatch_id.encode()).hexdigest()[:24]
        # Attempts are immutable.  A crashed worker may leave its private
        # O_EXCL descriptors behind, so a recovered attempt must never reuse
        # the prior capability, capsule, or result pathname.
        cap_path = self.runtime_root / f"capability-{dispatch_digest}-{attempt_suffix}.json"
        capsule_path = self.runtime_root / f"capsule-{dispatch_digest}-{attempt_suffix}.json"
        result_path = self.runtime_root / f"result-{dispatch_digest}-{attempt_suffix}.json"
        artifact_paths = (cap_path, capsule_path, result_path)
        if any(os.path.lexists(path) for path in artifact_paths):
            raise HarnessError("worker attempt artifacts already exist")
        capability_payload = {
            "version": 1,
            "operation": "submit_result",
            "dispatch_id": dispatch_id,
            "generation": row["generation"],
            "attempt": row["attempt"],
            "backend": row["backend"],
            "workspace_path": row["workspace_path"],
            "schema_sha256": row["result_contract_sha256"],
            "token": token,
            "socket_path": os.fspath(self.socket_path),
            "worker_role": "leaf",
            "allowed_operations": ["submit_result"],
            "leaf_worker_policy": policy,
            "effective_permission": effective_permission,
            "native_profile_sha256": native_profile_sha256,
            "native_compatibility_sha256": native_compatibility_sha256,
            "plugin_requirements": [item.to_json() for item in plugin_requirements],
            "plugin_capabilities": [
                {**item.to_json(), "capability_sha256": item.capability_digest} for item in plugin_snapshots
            ],
        }
        try:
            write_capability(cap_path, capability_payload)
        except FileExistsError:
            # O_EXCL reports a colliding descriptor that this invocation did
            # not create; leave it untouched for forensic inspection.
            raise
        except BaseException:
            # ``write_capability`` uses O_EXCL; record a path only when this
            # invocation created it so cleanup cannot remove a colliding file.
            if cap_path.is_file() and not cap_path.is_symlink():
                created_paths.append(cap_path)
            raise
        created_paths.append(cap_path)
        if recovery_continuation:
            original_prompt = capsule_value.get("prompt")
            if not isinstance(original_prompt, str):
                raise HarnessError("worker recovery capsule lacks its original prompt")
            capsule_value["prompt"] = recovery_continuation_prompt(
                workspace=Path(str(row["workspace_path"])),
                resume_same_thread=row.get("thread_id") is not None,
                original_prompt=original_prompt,
                observed_state="idle-no-result",
            )
        capsule_payload = json.dumps(capsule_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        capsule_fd: int | None = None
        try:
            capsule_fd = os.open(
                capsule_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
            )
            _write_once(capsule_fd, capsule_payload)
        except BaseException:
            if capsule_fd is not None and capsule_path.is_file() and not capsule_path.is_symlink():
                created_paths.append(capsule_path)
            raise
        finally:
            if capsule_fd is not None:
                os.close(capsule_fd)
        created_paths.append(capsule_path)
        self.ledger.set_queue_state(dispatch_id, "starting", epoch=self.epoch)
        command = (
            *self.worker_command,
            "--capability-file",
            os.fspath(cap_path),
            "--capsule-file",
            os.fspath(capsule_path),
            "--result-file",
            os.fspath(result_path),
            "--socket-path",
            os.fspath(self.socket_path),
        )
        thread_id = row.get("thread_id")
        if thread_id is not None:
            command += ("--resume-thread-id", str(thread_id))
        try:
            child = subprocess.Popen(command, start_new_session=True, close_fds=True)
        except OSError as exc:
            raise HarnessError("worker launch failed") from exc
        try:
            self.ledger.bind_worker_liveness(
                dispatch_id,
                epoch=int(self.epoch),
                generation=int(row["generation"]),
                attempt=int(row["attempt"]),
                pid=child.pid,
                process_birth_identity=process_birth_identity(child.pid),
                lease_token_sha256=token_hash,
                lease_seconds=self.lease_seconds,
            )
        except LedgerError:
            # A very fast worker can submit and terminalize between Popen and
            # the parent liveness bind.  Its already-authorized result owns
            # the terminal fact; do not turn that race into a fabricated
            # worker failure or kill the child after successful ingress.
            current = self.ledger.queue_dispatch(dispatch_id)
            if current["state"] not in {"completed", "failed", "cancelled"}:
                try:
                    child.terminate()
                except OSError:
                    pass
                raise
        except BaseException:
            try:
                child.terminate()
            except OSError:
                pass
            raise
        self._children[dispatch_id] = child
        if thread_id is not None:
            self._resumed_children.add(dispatch_id)

    def _close_failed_spawn(
        self,
        row: dict[str, object],
        created_paths: Sequence[Path],
        *,
        failure: BaseException,
    ) -> None:
        """Clean this spawn's private files and close any stranded claim."""

        dispatch_id = str(row["dispatch_id"])
        queue = self.ledger.queue_dispatch(dispatch_id)
        recovery = self.ledger.recovery_state(dispatch_id)
        if queue["state"] in {"queued", "claimed", "starting", "running"} and recovery["recovery_state"] == "none":
            if isinstance(failure, WorkerCompatibilityDrift):
                self.ledger.mark_human_attention_required(
                    dispatch_id,
                    reason=str(failure),
                    failure_class="profile",
                )
            else:
                self.ledger.mark_human_attention_required(
                    dispatch_id,
                    reason="worker spawn failed before a durable live-worker binding",
                )

        # Invocation-owned files are secondary to ledger authority.  Retain
        # them when terminalization fails so reconciliation never loses the
        # exact attempt facts it needs to diagnose the stranded claim.
        for path in created_paths:
            try:
                metadata = path.lstat()
            except OSError:
                continue
            # Never remove a path that became a link or was replaced by a
            # multi-link file after creation.  A stale colliding descriptor is
            # therefore retained as evidence rather than deleted blindly.
            if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
                continue
            try:
                path.unlink()
            except OSError:
                continue

    def process_once(self) -> bool:
        if self.epoch is None:
            self.acquire()
        self._renew_harness_lease_if_due(force=True)
        if self.ledger.harness_refresh_fenced():
            return False
        claim_nonce_hash = hashlib.sha256(os.urandom(32)).hexdigest()
        try:
            row = self.ledger.claim_queue_dispatch(epoch=int(self.epoch), claim_nonce_sha256=claim_nonce_hash)
        except HarnessRefreshBlocked:
            return False
        if row is None:
            return False
        self._spawn_one(row, recovery_continuation=int(row["attempt"]) > 1)
        self._refresh_checkpoint_deadline()
        return True

    def _process_queue_event(self) -> bool:
        """Keep one durably closed dispatch failure local to that dispatch.

        ``_spawn_one`` moves an ordinary preparation/launch failure to
        ``human_attention_required`` before re-raising.  The foreground
        service must preserve that fail-closed dispatch fact without turning
        it into a repository-wide harness outage.  Failure to commit that
        terminalization is different: it still escapes so systemd and an
        operator can see that lifecycle authority is uncertain.
        """

        try:
            return self.process_once()
        except FailedSpawnTerminalizationError:
            raise
        except HarnessError:
            return False

    def _recover_exited_dispatch(self, row: dict[str, object]) -> bool:
        """Inspect one exited worker's persisted SDK thread before writing."""

        dispatch_id = str(row["dispatch_id"])
        thread_id = row.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            self.ledger.unresolved_control_commands(dispatch_id)
            self.ledger.mark_human_attention_required(
                dispatch_id,
                reason="worker exited without a persisted SDK thread identity",
            )
            return True
        inspector = self._thread_inspector
        if inspector is None:
            inspector = self._default_thread_inspector
        if inspector is None:  # pragma: no cover - the default is always bound
            self.ledger.mark_human_attention_required(
                dispatch_id,
                reason="persisted-thread inspection capability is unavailable",
            )
            return True
        try:
            inspection = inspector(thread_id)
        except Exception:
            inspection = ThreadInspection(thread_id, ThreadInspectionKind.AMBIGUOUS, detail="inspection failed")
        if inspection.kind is ThreadInspectionKind.TERMINAL_RESULT:
            # A persisted completed turn proves that an interrupt command did
            # not receive the required interrupted terminal evidence.  Reject
            # only the exact sent interrupt; every other in-flight command is
            # unresolved because its application cannot be reconstructed.
            for command in self.ledger.control_commands(dispatch_id):
                if (
                    command.get("kind") == ControlCommandKind.INTERRUPT.value
                    and command.get("state") == ControlCommandState.SENT.value
                    and (inspection.turn_id is None or command.get("turn_id") == inspection.turn_id)
                ):
                    try:
                        self.ledger.acknowledge_control_command(
                            str(command["command_id"]),
                            state=ControlCommandState.REJECTED,
                            acknowledgement={
                                "terminal_status": "completed",
                                "detail": "SDK turn completed without interrupt terminal evidence",
                            },
                        )
                    except LedgerError:
                        pass
            self.ledger.unresolved_control_commands(dispatch_id)
            if not isinstance(inspection.raw_result, str):
                self.ledger.mark_human_attention_required(dispatch_id, reason="terminal inspection had no envelope")
            else:
                try:
                    row = self.ledger.queue_dispatch(dispatch_id)
                    self._assert_control_repair_preingestion_authority(row)
                    self.ledger.ingest_recovered_result(
                        dispatch_id,
                        raw_result=inspection.raw_result,
                    )
                except (LedgerError, ValueError, WorktreeError):
                    self.ledger.mark_human_attention_required(
                        dispatch_id,
                        reason="persisted-thread terminal envelope was invalid or conflicting",
                    )
            self._refresh_checkpoint_deadline()
            return True
        if inspection.kind is ThreadInspectionKind.INTERRUPTED:
            # The SDK terminal status is the only authority for confirming an
            # interrupt.  Preserve that evidence on the exact command while
            # stopping automatic recovery; no synthetic model result is
            # fabricated for an interrupted turn.
            for command in self.ledger.control_commands(dispatch_id):
                if (
                    command.get("kind") == ControlCommandKind.INTERRUPT.value
                    and command.get("state") == ControlCommandState.SENT.value
                    and (inspection.turn_id is None or command.get("turn_id") == inspection.turn_id)
                ):
                    try:
                        self.ledger.acknowledge_control_command(
                            str(command["command_id"]),
                            state=ControlCommandState.ACKNOWLEDGED,
                            acknowledgement={
                                "terminal_status": "interrupted",
                                "detail": "SDK turn ended with interrupted terminal evidence",
                            },
                        )
                    except LedgerError:
                        pass
            self.ledger.unresolved_control_commands(dispatch_id)
            self.ledger.mark_human_attention_required(
                dispatch_id,
                reason="SDK worker turn was interrupted; no terminal model result was produced",
            )
            return True
        if inspection.kind is ThreadInspectionKind.ACTIVE_WRITER:
            self.ledger.unresolved_control_commands(dispatch_id)
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(max(self.lease_seconds, 1.0), 300.0))
            self.ledger.record_recovery_inspection(
                dispatch_id,
                kind="active_writer",
                thread_id=thread_id,
                turn_id=inspection.turn_id,
                next_eligible_at=retry_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            )
            return True
        if inspection.kind is ThreadInspectionKind.IDLE_NO_RESULT:
            self.ledger.unresolved_control_commands(dispatch_id)
            self.ledger.record_recovery_inspection(
                dispatch_id,
                kind="idle_no_result",
                thread_id=thread_id,
                turn_id=inspection.turn_id,
                next_eligible_at=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            )
            return True
        if inspection.kind in {ThreadInspectionKind.EMPTY_HISTORY, ThreadInspectionKind.FAILED_TURN}:
            policy = self.ledger.retry_policy(dispatch_id)
            if (
                inspection.kind is ThreadInspectionKind.FAILED_TURN
                and policy.get("last_failure") in {"post_identity_loss", "provider_transient"}
                and policy.get("strategy") == "same_thread_continuation"
                and (
                    0
                    < int(
                        policy.get(
                            "provider_transient_used"
                            if policy.get("last_failure") == "provider_transient"
                            else "post_identity_loss_used",
                            0,
                        )
                    )
                    <= int(
                        policy.get(
                            "provider_transient_budget"
                            if policy.get("last_failure") == "provider_transient"
                            else "post_identity_loss_budget",
                            0,
                        )
                    )
                )
            ):
                self.ledger.unresolved_control_commands(dispatch_id)
                try:
                    next_eligible_at = _merged_recovery_deadline(
                        policy.get("next_eligible_at"),
                        inspection.retry_at if policy.get("last_failure") == "provider_transient" else None,
                        fallback=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    )
                except ValueError:
                    self.ledger.mark_human_attention_required(
                        dispatch_id,
                        reason="typed provider recovery deadline was malformed",
                        failure_class="provider_transient",
                    )
                    return True
                self.ledger.record_recovery_inspection(
                    dispatch_id,
                    kind="transient_failed_turn",
                    thread_id=thread_id,
                    turn_id=inspection.turn_id,
                    next_eligible_at=next_eligible_at,
                )
                return True
            if (
                policy.get("last_failure") == "invalid_response_chain"
                and policy.get("strategy") == "fresh_thread"
                and 0 < int(policy.get("invalid_chain_used", 0)) <= int(policy.get("invalid_chain_budget", 0))
            ):
                self.ledger.unresolved_control_commands(dispatch_id)
                self.ledger.record_recovery_inspection(
                    dispatch_id,
                    kind=(
                        "invalid_chain_empty_history"
                        if inspection.kind is ThreadInspectionKind.EMPTY_HISTORY
                        else "invalid_chain_failed_turn"
                    ),
                    thread_id=thread_id,
                    turn_id=inspection.turn_id,
                    next_eligible_at=datetime.now(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                )
                return True
            self.ledger.unresolved_control_commands(dispatch_id)
            self.ledger.mark_human_attention_required(
                dispatch_id,
                reason="persisted-thread failed or empty history lacks typed invalid-chain authority",
            )
            return True
        self.ledger.unresolved_control_commands(dispatch_id)
        self.ledger.mark_human_attention_required(
            dispatch_id,
            reason="persisted-thread inspection was ambiguous or malformed",
        )
        return True

    def _default_thread_inspector(self, thread_id: str) -> ThreadInspection:
        """Inspect one persisted thread through the shared SDK session."""

        adapter = CodexSdkAdapter(
            CodexSdkConfig(
                model="recovery-inspection",
                reasoning_effort=ReasoningEffort.NONE,
                sandbox=Sandbox.READ_ONLY,
                cwd=self.state_root,
            )
        )
        try:
            return adapter.inspect_persisted_thread(ThreadIdentity(thread_id))
        finally:
            adapter.close()

    def recover_once(self) -> None:
        """Reconcile one restart snapshot without duplicating live work."""

        if self.ledger.harness_refresh_fenced():
            return
        self.ledger.reconcile_inflight_wakes()
        self.ledger.reconcile_exhausted_wake_notifications()
        self._advance_pending_cancellations()
        for row in self.ledger.queue_dispatches():
            if row.get("state") == "result_submitted":
                try:
                    finalized = self.ledger.finalize_queue_result(str(row["dispatch_id"]))
                    self._record_program_queue_result(finalized)
                except LedgerError:
                    pass

        # A crash may have occurred after queue terminalization but before the
        # event-driven program projection was recorded.  This is a bounded
        # startup recovery scan, not a steady-state poll.
        for row in self.ledger.queue_dispatches():
            if row.get("state") in {"completed", "failed"}:
                self._record_program_queue_result(row)

        # A worker can finish and persist its exact result while synchronous
        # diagnostic traffic delays IPC until after the exit classification.
        # Reconcile only that typed, exited attempt from its private immutable
        # artifacts; never inspect or resume the provider thread for this case.
        for row in self.ledger.queue_dispatches():
            if row.get("state") != "human_attention_required":
                continue
            try:
                self._recover_retained_worker_result(row)
            except (LedgerError, WorkerError, ValueError):
                continue

        processed_inspections: set[str] = set()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for pending in self.ledger.queue_dispatches():
            recovery = self.ledger.recovery_state(str(pending["dispatch_id"]))
            if recovery.get("recovery_state") != "recovery_retry_wait":
                continue
            eligible = recovery.get("next_eligible_at")
            if isinstance(eligible, str) and eligible > now:
                continue
            try:
                self.ledger.begin_recovery_inspection(str(pending["dispatch_id"]), epoch=int(self.epoch), now=now)
            except LedgerError:
                continue

        for row in self.ledger.queue_dispatches():
            if row.get("state") not in {"claimed", "starting", "running"}:
                continue
            dispatch_id = str(row["dispatch_id"])
            child = getattr(self, "_children", {}).get(dispatch_id)
            if row.get("claim_epoch") == self.epoch and child is not None and child.poll() is None:
                continue
            live = self.ledger.worker_liveness(dispatch_id)
            if live is not None and live.get("exited_at") is None:
                try:
                    os.kill(int(live["pid"]), 0)
                    alive = process_birth_identity(int(live["pid"])) == str(live["process_birth_identity"])
                except OSError:
                    alive = False
                if alive:
                    if row.get("claim_epoch") == self.epoch:
                        continue
                    try:
                        self.ledger.adopt_live_worker(
                            dispatch_id,
                            new_epoch=int(self.epoch),
                            pid=int(live["pid"]),
                            process_birth_identity=str(live["process_birth_identity"]),
                            claim_nonce_sha256=hashlib.sha256(os.urandom(32)).hexdigest(),
                        )
                    except LedgerError:
                        pass
                    continue
            if live is not None and live.get("exited_at") is not None:
                try:
                    if self._recover_exited_dispatch(row):
                        processed_inspections.add(str(row["dispatch_id"]))
                        continue
                except LedgerError:
                    continue
            if live is None:
                try:
                    self.ledger.mark_human_attention_required(
                        dispatch_id,
                        reason="active queue dispatch has no persisted worker liveness identity",
                    )
                except (LedgerError, ValueError):
                    pass
                continue
            # A restart found a bound worker process that is no longer alive,
            # but its exit was not durably observed.  Record that fact first;
            # the same inspect-before-mutate recovery boundary then decides
            # whether to wait, continue, or require a human.
            try:
                self.ledger.mark_worker_exit(
                    str(row["dispatch_id"]),
                    pid=int(live["pid"]),
                    process_birth_identity=str(live["process_birth_identity"]),
                    classification="restart-observed-worker-dead",
                )
                refreshed = self.ledger.queue_dispatch(str(row["dispatch_id"]))
                if self._recover_exited_dispatch(refreshed):
                    processed_inspections.add(str(row["dispatch_id"]))
            except (LedgerError, ValueError):
                continue

        for row in self.ledger.queue_dispatches():
            if row.get("state") != "recovery_inspection_pending":
                continue
            if str(row["dispatch_id"]) in processed_inspections:
                continue
            try:
                self._recover_exited_dispatch(row)
            except LedgerError:
                continue

        # A continuation is claimable only after the preceding read-only
        # inspection committed ``recovery_continuation_pending``.  This path
        # consumes one durable continuation budget before creating a writer.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for row in self.ledger.queue_dispatches():
            recovery = self.ledger.recovery_state(str(row["dispatch_id"]))
            if recovery.get("recovery_state") != "recovery_continuation_pending":
                continue
            eligible = recovery.get("next_eligible_at")
            if isinstance(eligible, str) and eligible > now:
                continue
            try:
                claimed = self.ledger.begin_recovery_continuation(
                    str(row["dispatch_id"]),
                    epoch=int(self.epoch),
                    claim_nonce_sha256=hashlib.sha256(os.urandom(32)).hexdigest(),
                    now=now,
                )
                if claimed.get("state") == "claimed":
                    self._spawn_one(claimed, recovery_continuation=True)
            except FailedSpawnTerminalizationError:
                raise
            except (LedgerError, HarnessError):
                continue

    def _recover_retained_worker_result(self, row: dict[str, object]) -> bool:
        """Ingest one exact result artifact from an exited transport attempt."""

        dispatch_id = str(row["dispatch_id"])
        generation = int(row["generation"])
        attempt = int(row["attempt"])
        runtime_root = getattr(self, "runtime_root", None)
        if not isinstance(runtime_root, Path):
            return False
        dispatch_digest = hashlib.sha256(dispatch_id.encode()).hexdigest()[:24]
        suffix = f"g{generation}-a{attempt}"
        capability_path = runtime_root / f"capability-{dispatch_digest}-{suffix}.json"
        result_path = runtime_root / f"result-{dispatch_digest}-{suffix}.json"
        if not os.path.lexists(capability_path) or not os.path.lexists(result_path):
            return False
        binding = _read_private_capability(capability_path)
        capability = binding.payload
        expected = {
            "dispatch_id": dispatch_id,
            "generation": generation,
            "attempt": attempt,
            "backend": row["backend"],
            "workspace_path": row["workspace_path"],
            "schema_sha256": row["result_contract_sha256"],
        }
        if any(capability.get(key) != value for key, value in expected.items()):
            raise WorkerError("retained worker capability does not match queue identity")
        raw_result = _read_private_file(result_path, max_bytes=65_536, require_readonly=True)
        self._assert_control_repair_preingestion_authority(row)
        terminal = self.ledger.commit_retained_queue_result(
            dispatch_id,
            generation=generation,
            attempt=attempt,
            token=str(capability["token"]),
            raw_result=raw_result,
        )
        self._record_program_queue_result(terminal)
        return terminal["state"] in {"completed", "failed"}

    def _deliver_wakes(self) -> bool:
        """Deliver pending harness envelopes without reading Codex task state."""

        changed = False
        for wake in self.ledger.wake_outbox(state="pending"):
            delivery_id = str(wake["delivery_id"])
            while True:
                claimed = self.ledger.claim_wake(delivery_id)
                if claimed is None or claimed["state"] != "starting":
                    break
                source_thread_id = claimed.get("source_thread_id")
                if not isinstance(source_thread_id, str):
                    break
                try:
                    if self._wake_delivery is None:
                        turn_id = CodexSdkNotifier().deliver(source_thread_id, str(claimed["payload_json"]))
                    else:
                        turn_id = self._wake_delivery(source_thread_id, str(claimed["payload_json"]))
                except WakeDeliveryAmbiguous:
                    outcome = self.ledger.record_wake_delivery(delivery_id, outcome="ambiguous")
                except (WakeDeliveryUnavailable, OSError, RuntimeError, ValueError):
                    outcome = self.ledger.record_wake_delivery(delivery_id, outcome="failed")
                else:
                    outcome = self.ledger.record_wake_delivery(delivery_id, outcome="delivered", source_turn_id=turn_id)
                changed = True
                # Source notification is best-effort and has a ledger-owned
                # two-attempt ceiling. Exhaust that bounded budget in the
                # triggering event so a first unavailable call cannot strand
                # the controller decision until an unrelated socket event.
                if outcome["state"] != "pending":
                    break
        return changed

    def _renew_harness_lease_if_due(self, *, force: bool = False) -> None:
        """Renew before post-IPC lifecycle work while preserving exact epoch ownership."""

        epoch = getattr(self, "epoch", None)
        owner_nonce_sha256 = getattr(self, "owner_nonce_sha256", None)
        lease_seconds = getattr(self, "lease_seconds", None)
        if epoch is None or not isinstance(owner_nonce_sha256, str) or not isinstance(lease_seconds, int | float):
            return
        now_monotonic = time.monotonic()
        next_renewal = getattr(self, "_next_renewal_monotonic", None)
        if not force and next_renewal is not None and now_monotonic < next_renewal:
            return
        self.ledger.renew_harness(
            epoch=epoch,
            owner_nonce_sha256=owner_nonce_sha256,
            lease_seconds=lease_seconds,
        )
        self._next_renewal_monotonic = now_monotonic + max(lease_seconds / 3.0, 0.1)

    def run_foreground(self, *, timeout: float = 0.25, max_cycles: int | None = None) -> None:
        try:
            self.acquire()
            self.recover_once()
            self._refresh_checkpoint_deadline()
            self._next_renewal_monotonic = time.monotonic() + max(self.lease_seconds / 3.0, 0.1)
            self._process_queue_event()
            self._deliver_wakes()
            self._schedule_controller_generations()
            cycles = 0
            while not self._stop and (max_cycles is None or cycles < max_cycles):
                cycles += 1
                endpoint = self._socket
                if endpoint is None:
                    break
                ready, _, _ = select.select([endpoint], [], [], self._select_timeout(timeout))
                if ready:
                    connection, _ = endpoint.accept()
                    operation = self._accept_connection(connection)
                    self._renew_harness_lease_if_due()
                    # Queue and controller action ingress are the only normal
                    # steady-state lifecycle triggers.  In particular,
                    # worker_event, heartbeat, status, and control polling
                    # remain cheap IPC acknowledgements and never cause a
                    # repository-wide queue/controller scan.
                    if operation in QUEUE_TRIGGERING_OPERATIONS:
                        self._process_queue_event()
                        self._deliver_wakes()
                        self._schedule_controller_generations()
                        self._refresh_checkpoint_deadline()
                now_monotonic = time.monotonic()
                if self._next_renewal_monotonic is not None and now_monotonic >= self._next_renewal_monotonic:
                    self._renew_harness_lease_if_due()
                    children_reaped = self._reap_children()
                    controller_reaped = self._reap_controller_generations()
                    if children_reaped:
                        self.recover_once()
                        self._process_queue_event()
                    if children_reaped or controller_reaped:
                        self._deliver_wakes()
                        self._schedule_controller_generations()
                    self._refresh_checkpoint_deadline()
                if (
                    self._next_checkpoint_deadline is not None
                    and self._next_checkpoint_deadline <= datetime.now(timezone.utc)
                    and self.epoch is not None
                ):
                    checkpoint = self.ledger.claim_due_checkpoint(epoch=int(self.epoch))
                    if checkpoint is not None:
                        self._deliver_wakes()
                        self._schedule_controller_generations()
                    self._refresh_checkpoint_deadline()
        finally:
            self.close()


__all__ = ["HarnessError", "WorkerResultRejected", "WorkflowHarness", "process_birth_identity"]
