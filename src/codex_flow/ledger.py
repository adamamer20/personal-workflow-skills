"""The durable, local source of truth for Codex workflow state.

H2 deliberately keeps this module small and boring: the standard-library
``sqlite3`` connection is the only persistence boundary, writes are explicit
transactions, and every state mutation appends its event before commit.  No
SDK, subprocess, network, or worktree code belongs here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

from .app_native import (
    AppNativeDispatchRecord,
    AppNativeState,
    AppNativeTaskAction,
    HostIdentity,
    HostReceipt,
    claim_token_sha256,
)
from .contracts import (
    ModelFacingControllerActionBundle,
    ModelFacingProgramControllerActionBundle,
    ModelFacingResult,
    ModelResultStatus,
    legacy_model_facing_result_schema_sha256,
    model_facing_result_schema_sha256,
    model_facing_review_result_schema_sha256,
    review_result_from_agent_message,
    review_result_to_json,
)
from .domain import (
    CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES,
    DIAGNOSTIC_RING_MAX_BYTES,
    DIAGNOSTIC_RING_MAX_ENTRIES,
    DIAGNOSTIC_TEXT_MAX_BYTES,
    TERMINAL_STATES,
    AcceptanceMode,
    BlockerKind,
    BlockerScope,
    Budget,
    BudgetExhaustion,
    CandidateDisposition,
    CandidateRecord,
    CompatibilityRebind,
    ControlCommand,
    ControlCommandKind,
    ControlCommandState,
    ControllerActionKind,
    ControllerActionReceipt,
    ControllerCheckpoint,
    ControllerClaimantKind,
    ControllerDecisionClaim,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerDecisionStatus,
    ControllerDecisionSummary,
    ControllerGenerationState,
    ControllerGenerationStatus,
    ControllerRecoveryInspectionClaim,
    ControlListKind,
    ControlListVisibility,
    DecisionRequest,
    DecisionResponse,
    DiagnosticEvent,
    DispatchClaim,
    DispatchId,
    EventId,
    EventRecord,
    EventSequence,
    ExecutionCapsule,
    ExecutionIntegrityRecord,
    ExecutionRecord,
    ExecutionStatus,
    Generation,
    JsonObject,
    LedgerSnapshot,
    LifecycleEvent,
    LifecyclePhase,
    LifecycleRecord,
    MilestoneId,
    MilestoneRecord,
    NativePermissionAuthority,
    NativePermissionMode,
    PostIdentityExecutionFailure,
    PreIdentityTransportFailure,
    ProgramControllerActionKind,
    ProgramControllerContext,
    ProgramControllerDecisionStatus,
    ProgramControllerNodeContext,
    ProgramEventKind,
    ProgramGraph,
    ProgramId,
    ProgramNodeSpec,
    ProgramNodeStatus,
    ProgramState,
    ProgramStatus,
    ReasonCode,
    ReasoningEffort,
    RecoveryActionKind,
    RecoveryDecision,
    RecoveryFact,
    RecoveryStrategy,
    RetryBudgetChange,
    RetryFailureClass,
    ReviewRejected,
    RoleId,
    RunId,
    RunRecord,
    SchemaVersion,
    TerminalFailureAfterIdentity,
    TerminalOutcome,
    ThreadIdentity,
    TransportFailureBeforeIdentity,
    TurnObservation,
    TypedBlocker,
    ValidationFailureCode,
    ValidationObservation,
    WorkflowReason,
    WorkflowState,
    WorkspaceLeaseRecord,
    WorkspaceMode,
    blocker_applies_to,
    coerce_state,
    is_transition_allowed,
    redact_control_text,
    redact_diagnostic_text,
    strict_json_loads,
)

CURRENT_SCHEMA_VERSION = SchemaVersion(19)
SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        SchemaVersion(1),
        SchemaVersion(2),
        SchemaVersion(3),
        SchemaVersion(4),
        SchemaVersion(5),
        SchemaVersion(6),
        SchemaVersion(7),
        SchemaVersion(8),
        SchemaVersion(9),
        SchemaVersion(10),
        SchemaVersion(11),
        SchemaVersion(12),
        SchemaVersion(13),
        SchemaVersion(14),
        SchemaVersion(15),
        SchemaVersion(16),
        SchemaVersion(17),
        SchemaVersion(18),
        CURRENT_SCHEMA_VERSION,
    }
)
_STATES_SQL = ", ".join(f"'{state.value}'" for state in WorkflowState)
_REASON_CODES_SQL = ", ".join(f"'{reason.value}'" for reason in ReasonCode)
_EVENT_TYPES = frozenset({"dispatch_claimed", "state_transition"})
_EVENT_TYPES_SQL = ", ".join(f"'{event_type}'" for event_type in sorted(_EVENT_TYPES))
_TURN_STARTING_MARKER: JsonObject = {"__controller_checkpoint": "turn_starting"}
_CONTROLLER_DECISION_WINDOW_SECONDS = 300.0
# Result capabilities authorize one exact attempt, not its scheduling
# checkpoint.  The descriptor starts with a bounded lifetime and is extended
# only by the already-authorized worker lease heartbeat while the attempt is
# active.  This keeps a recovered attempt usable without allowing an
# unbounded token lifetime.
_ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS = 30.0
_ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS = 300.0
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }
)

_SCHEMA_META_DDL = """CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY NOT NULL CHECK(length(key) > 0),
    value TEXT NOT NULL CHECK(length(value) > 0)
)"""
_RUNS_V1_DDL = """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(metadata_json = '{}')
)"""
_RUNS_V2_DDL = """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(metadata_json = '{}')
)"""
_MILESTONES_DDL = f"""CREATE TABLE milestones (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    current_state TEXT NOT NULL CHECK(current_state IN ({_STATES_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(metadata_json = '{{}}'),
    PRIMARY KEY(run_id, milestone_id),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
)"""
_DISPATCHES_DDL = """CREATE TABLE dispatches (
    dispatch_id TEXT PRIMARY KEY NOT NULL CHECK(length(dispatch_id) BETWEEN 1 AND 512),
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(length(role) BETWEEN 1 AND 128),
    generation INTEGER NOT NULL CHECK(generation > 0),
    claimed_at TEXT NOT NULL,
    CHECK(dispatch_id = run_id || '/' || milestone_id || '/' || role || '/' || generation),
    UNIQUE(run_id, milestone_id, role),
    FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
)"""
_EVENTS_DDL = f"""CREATE TABLE events (
    event_id TEXT PRIMARY KEY NOT NULL CHECK(length(event_id) BETWEEN 1 AND 256),
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    from_state TEXT CHECK(from_state IS NULL OR from_state IN ({_STATES_SQL})),
    to_state TEXT NOT NULL CHECK(to_state IN ({_STATES_SQL})),
    event_type TEXT NOT NULL CHECK(event_type IN ({_EVENT_TYPES_SQL})),
    reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN ({_REASON_CODES_SQL})),
    reason_detail TEXT CHECK(reason_detail IS NULL),
    dispatch_id TEXT,
    data_json TEXT CHECK(data_json IS NULL),
    occurred_at TEXT NOT NULL,
    CHECK(event_id = run_id || '/' || milestone_id || '/' || sequence),
    UNIQUE(run_id, milestone_id, sequence),
    FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE,
    FOREIGN KEY(dispatch_id) REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT
)"""
_V1_TABLE_DDL = {
    "schema_meta": _SCHEMA_META_DDL,
    "runs": _RUNS_V1_DDL,
    "milestones": _MILESTONES_DDL,
    "dispatches": _DISPATCHES_DDL,
    "events": _EVENTS_DDL,
}
_V2_TABLE_DDL = {**_V1_TABLE_DDL, "runs": _RUNS_V2_DDL}

_EXECUTION_STATUSES_SQL = ", ".join(f"'{status.value}'" for status in ExecutionStatus)
_CHECKPOINTS_SQL = ", ".join(f"'{checkpoint.value}'" for checkpoint in ControllerCheckpoint)
_WORKSPACE_MODES_SQL = ", ".join(f"'{mode.value}'" for mode in WorkspaceMode)
_WORKSPACE_LEASES_DDL = f"""CREATE TABLE workspace_leases (
    workspace_path TEXT PRIMARY KEY NOT NULL CHECK(length(workspace_path) > 1),
    repository_root TEXT NOT NULL CHECK(length(repository_root) > 1),
    mode TEXT NOT NULL CHECK(mode IN ({_WORKSPACE_MODES_SQL})),
    branch TEXT NOT NULL CHECK(length(branch) > 0),
    base_sha TEXT NOT NULL CHECK(length(base_sha) = 40),
    lane TEXT NOT NULL CHECK(length(lane) BETWEEN 1 AND 128),
    owner_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_run_id, lane),
    FOREIGN KEY(owner_run_id) REFERENCES runs(run_id) ON DELETE CASCADE
)"""
_EXECUTIONS_DDL = f"""CREATE TABLE executions (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    capsule_path TEXT NOT NULL CHECK(length(capsule_path) > 1),
    capsule_sha256 TEXT NOT NULL CHECK(length(capsule_sha256) = 64),
    workspace_path TEXT NOT NULL CHECK(length(workspace_path) > 1),
    model TEXT NOT NULL CHECK(length(model) > 0),
    reasoning_effort TEXT NOT NULL CHECK(length(reasoning_effort) > 0),
    status TEXT NOT NULL CHECK(status IN ({_EXECUTION_STATUSES_SQL})),
    checkpoint TEXT NOT NULL CHECK(checkpoint IN ({_CHECKPOINTS_SQL})),
    thread_id TEXT,
    turn_id TEXT,
    turn_output_json TEXT,
    result_json TEXT,
    validation_argv_json TEXT,
    validation_exit_code INTEGER,
    validation_stdout_sha256 TEXT,
    validation_stderr_sha256 TEXT,
    validation_timed_out INTEGER CHECK(validation_timed_out IN (0, 1)),
    validation_duration_seconds REAL CHECK(validation_duration_seconds >= 0),
    protected_before_sha256 TEXT NOT NULL CHECK(length(protected_before_sha256) = 64),
    protected_after_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    FOREIGN KEY(run_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
)"""
_SDK_EVENTS_DDL = """CREATE TABLE sdk_lifecycle_events (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    turn_id TEXT NOT NULL CHECK(length(turn_id) > 0),
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    method TEXT NOT NULL CHECK(length(method) > 0),
    event_turn_id TEXT,
    PRIMARY KEY(run_id, milestone_id, turn_id, sequence),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V3_TABLE_DDL = {
    **_V2_TABLE_DDL,
    "workspace_leases": _WORKSPACE_LEASES_DDL,
    "executions": _EXECUTIONS_DDL,
    "sdk_lifecycle_events": _SDK_EVENTS_DDL,
}
_EXECUTION_INTEGRITY_V4_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'controller_v1')),
    sandbox_policy_sha256 TEXT CHECK(sandbox_policy_sha256 IS NULL OR length(sandbox_policy_sha256) = 64),
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance = 'legacy_v3' AND sandbox_policy_sha256 IS NULL AND git_authority_before_sha256 IS NULL AND git_authority_after_sha256 IS NULL)
       OR (provenance = 'controller_v1' AND sandbox_policy_sha256 IS NOT NULL)),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V4_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_V4_DDL}
_EXECUTION_INTEGRITY_V5_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'legacy_sandbox_v4', 'controller_v2')),
    native_profile_sha256 TEXT CHECK(native_profile_sha256 IS NULL OR length(native_profile_sha256) = 64),
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance IN ('legacy_v3', 'legacy_sandbox_v4') AND native_profile_sha256 IS NULL)
       OR (provenance = 'controller_v2' AND native_profile_sha256 IS NOT NULL)),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V5_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_V5_DDL}
_EXECUTION_INTEGRITY_V6_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'legacy_sandbox_v4', 'legacy_profile_v5', 'controller_v3')),
    native_profile_sha256 TEXT CHECK(native_profile_sha256 IS NULL OR length(native_profile_sha256) = 64),
    native_compatibility_sha256 TEXT CHECK(native_compatibility_sha256 IS NULL OR length(native_compatibility_sha256) = 64),
    effective_permission_json TEXT CHECK(effective_permission_json IS NULL OR length(effective_permission_json) BETWEEN 1 AND 512),
    effective_permission_sha256 TEXT CHECK(effective_permission_sha256 IS NULL OR length(effective_permission_sha256) = 64),
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance IN ('legacy_v3', 'legacy_sandbox_v4') AND native_profile_sha256 IS NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL)
       OR (provenance = 'legacy_profile_v5' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL)
       OR (provenance = 'controller_v3' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NOT NULL AND effective_permission_json IS NOT NULL
           AND effective_permission_sha256 IS NOT NULL)),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V6_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_V6_DDL}
_EXECUTION_INTEGRITY_V7_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'legacy_sandbox_v4', 'legacy_profile_v5', 'legacy_permission_v6', 'controller_v4')),
    native_profile_sha256 TEXT CHECK(native_profile_sha256 IS NULL OR length(native_profile_sha256) = 64),
    native_compatibility_sha256 TEXT CHECK(native_compatibility_sha256 IS NULL OR length(native_compatibility_sha256) = 64),
    effective_permission_json TEXT CHECK(effective_permission_json IS NULL OR length(effective_permission_json) BETWEEN 1 AND 512),
    effective_permission_sha256 TEXT CHECK(effective_permission_sha256 IS NULL OR length(effective_permission_sha256) = 64),
    workspace_baseline_head_sha TEXT CHECK(workspace_baseline_head_sha IS NULL OR length(workspace_baseline_head_sha) = 40),
    workspace_baseline_json TEXT,
    workspace_baseline_sha256 TEXT CHECK(workspace_baseline_sha256 IS NULL OR length(workspace_baseline_sha256) = 64),
    turn_started_at TEXT,
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance IN ('legacy_v3', 'legacy_sandbox_v4') AND native_profile_sha256 IS NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance = 'legacy_profile_v5' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance = 'legacy_permission_v6' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NOT NULL AND effective_permission_json IS NOT NULL
           AND effective_permission_sha256 IS NOT NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance = 'controller_v4' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NOT NULL AND effective_permission_json IS NOT NULL
           AND effective_permission_sha256 IS NOT NULL AND workspace_baseline_head_sha IS NOT NULL
           AND workspace_baseline_json IS NOT NULL AND workspace_baseline_sha256 IS NOT NULL)),
    CHECK(turn_started_at IS NULL OR length(turn_started_at) > 0),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V7_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_V7_DDL}

_EXECUTION_INTEGRITY_DDL = """CREATE TABLE execution_integrity (
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('legacy_v3', 'legacy_sandbox_v4', 'legacy_profile_v5', 'legacy_permission_v6', 'controller_v4', 'controller_v5')),
    native_profile_sha256 TEXT CHECK(native_profile_sha256 IS NULL OR length(native_profile_sha256) = 64),
    native_compatibility_sha256 TEXT CHECK(native_compatibility_sha256 IS NULL OR length(native_compatibility_sha256) = 64),
    effective_permission_json TEXT CHECK(effective_permission_json IS NULL OR length(effective_permission_json) BETWEEN 1 AND 512),
    effective_permission_sha256 TEXT CHECK(effective_permission_sha256 IS NULL OR length(effective_permission_sha256) = 64),
    workspace_baseline_head_sha TEXT CHECK(workspace_baseline_head_sha IS NULL OR length(workspace_baseline_head_sha) = 40),
    workspace_baseline_json TEXT,
    workspace_baseline_sha256 TEXT CHECK(workspace_baseline_sha256 IS NULL OR length(workspace_baseline_sha256) = 64),
    workspace_terminal_head_sha TEXT CHECK(workspace_terminal_head_sha IS NULL OR length(workspace_terminal_head_sha) = 40),
    workspace_terminal_json TEXT,
    workspace_terminal_sha256 TEXT CHECK(workspace_terminal_sha256 IS NULL OR length(workspace_terminal_sha256) = 64),
    turn_started_at TEXT,
    git_authority_before_sha256 TEXT CHECK(git_authority_before_sha256 IS NULL OR length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, milestone_id),
    CHECK((provenance IN ('legacy_v3', 'legacy_sandbox_v4') AND native_profile_sha256 IS NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance = 'legacy_profile_v5' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NULL AND effective_permission_json IS NULL
           AND effective_permission_sha256 IS NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance = 'legacy_permission_v6' AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NOT NULL AND effective_permission_json IS NOT NULL
           AND effective_permission_sha256 IS NOT NULL AND workspace_baseline_head_sha IS NULL
           AND workspace_baseline_json IS NULL AND workspace_baseline_sha256 IS NULL)
       OR (provenance IN ('controller_v4', 'controller_v5') AND native_profile_sha256 IS NOT NULL
           AND native_compatibility_sha256 IS NOT NULL AND effective_permission_json IS NOT NULL
           AND effective_permission_sha256 IS NOT NULL AND workspace_baseline_head_sha IS NOT NULL
           AND workspace_baseline_json IS NOT NULL AND workspace_baseline_sha256 IS NOT NULL)),
    CHECK((workspace_terminal_head_sha IS NULL AND workspace_terminal_json IS NULL AND workspace_terminal_sha256 IS NULL)
       OR (workspace_terminal_head_sha IS NOT NULL AND workspace_terminal_json IS NOT NULL AND workspace_terminal_sha256 IS NOT NULL)),
    CHECK(turn_started_at IS NULL OR length(turn_started_at) > 0),
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_V8_TABLE_DDL = {**_V3_TABLE_DDL, "execution_integrity": _EXECUTION_INTEGRITY_DDL}

_APP_NATIVE_DISPATCHES_DDL = """CREATE TABLE app_native_dispatches (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared', 'bound', 'completed', 'failed', 'cancelled')),
    action_json TEXT NOT NULL CHECK(length(action_json) > 0),
    action_sha256 TEXT NOT NULL CHECK(length(action_sha256) = 64),
    claim_token_sha256 TEXT NOT NULL CHECK(length(claim_token_sha256) = 64),
    host_id TEXT CHECK(host_id IS NULL OR length(host_id) BETWEEN 1 AND 512),
    thread_id TEXT CHECK(thread_id IS NULL OR length(thread_id) BETWEEN 1 AND 512),
    result_json TEXT,
    result_sha256 TEXT CHECK(result_sha256 IS NULL OR length(result_sha256) = 64),
    workspace_baseline_head_sha TEXT NOT NULL CHECK(length(workspace_baseline_head_sha) = 40),
    workspace_baseline_json TEXT NOT NULL,
    workspace_baseline_sha256 TEXT NOT NULL CHECK(length(workspace_baseline_sha256) = 64),
    git_authority_before_sha256 TEXT NOT NULL CHECK(length(git_authority_before_sha256) = 64),
    git_authority_after_sha256 TEXT CHECK(git_authority_after_sha256 IS NULL OR length(git_authority_after_sha256) = 64),
    controller_state_sha256 TEXT NOT NULL CHECK(length(controller_state_sha256) = 64),
    workspace_terminal_head_sha TEXT CHECK(workspace_terminal_head_sha IS NULL OR length(workspace_terminal_head_sha) = 40),
    workspace_terminal_json TEXT,
    workspace_terminal_sha256 TEXT CHECK(workspace_terminal_sha256 IS NULL OR length(workspace_terminal_sha256) = 64),
    prepared_at TEXT NOT NULL,
    bound_at TEXT,
    completed_at TEXT,
    UNIQUE(run_id, milestone_id),
    CHECK((host_id IS NULL AND thread_id IS NULL) OR (host_id IS NOT NULL AND thread_id IS NOT NULL)),
    CHECK((result_json IS NULL AND result_sha256 IS NULL) OR (result_json IS NOT NULL AND result_sha256 IS NOT NULL)),
    CHECK((workspace_terminal_head_sha IS NULL AND workspace_terminal_json IS NULL AND workspace_terminal_sha256 IS NULL)
       OR (workspace_terminal_head_sha IS NOT NULL AND workspace_terminal_json IS NOT NULL AND workspace_terminal_sha256 IS NOT NULL)),
    CHECK((state = 'prepared' AND host_id IS NULL AND result_json IS NULL AND bound_at IS NULL AND completed_at IS NULL)
       OR (state = 'bound' AND host_id IS NOT NULL AND result_json IS NULL AND bound_at IS NOT NULL AND completed_at IS NULL)
       OR (state IN ('completed', 'failed') AND host_id IS NOT NULL AND result_json IS NOT NULL
           AND bound_at IS NOT NULL AND completed_at IS NOT NULL AND git_authority_after_sha256 IS NOT NULL)
       OR (state = 'cancelled' AND result_json IS NULL AND completed_at IS NOT NULL
           AND ((host_id IS NULL AND bound_at IS NULL) OR (host_id IS NOT NULL AND bound_at IS NOT NULL)))),
    FOREIGN KEY(dispatch_id) REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT,
    FOREIGN KEY(run_id, milestone_id) REFERENCES executions(run_id, milestone_id) ON DELETE CASCADE
)"""
_APP_NATIVE_DISPATCHES_DRAFT_V9_DDL = _APP_NATIVE_DISPATCHES_DDL.replace(", 'cancelled'", "").replace(
    "\n       OR (state = 'cancelled' AND result_json IS NULL AND completed_at IS NOT NULL"
    "\n           AND ((host_id IS NULL AND bound_at IS NULL) OR (host_id IS NOT NULL AND bound_at IS NOT NULL)))",
    "",
)
_V9_TABLE_DDL = {**_V8_TABLE_DDL, "app_native_dispatches": _APP_NATIVE_DISPATCHES_DDL}

# H6-E durable queue/harness authority.  These tables deliberately keep
# immutable dispatch facts and process/attempt leases separate from the legacy
# H2-H6 execution projection.  The controller remains the sole writer and all
# token plaintext is kept outside SQLite in a descriptor-anchored capability
# file.
_SUPERVISOR_AUTHORITY_DDL = """CREATE TABLE supervisor_authority (
    singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
    repository_root TEXT NOT NULL CHECK(length(repository_root) > 1),
    state_root TEXT NOT NULL CHECK(length(state_root) > 1),
    epoch INTEGER NOT NULL CHECK(epoch > 0),
    owner_nonce_sha256 TEXT NOT NULL CHECK(length(owner_nonce_sha256) = 64),
    pid INTEGER NOT NULL CHECK(pid > 0),
    process_birth_identity TEXT NOT NULL CHECK(length(process_birth_identity) > 0),
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    executable_digest TEXT NOT NULL CHECK(length(executable_digest) = 64),
    version TEXT NOT NULL CHECK(length(version) > 0),
    requested_shutdown INTEGER NOT NULL CHECK(requested_shutdown IN (0, 1))
)"""
_DISPATCH_QUEUE_DDL = """CREATE TABLE dispatch_queue (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    role TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    backend TEXT NOT NULL CHECK(backend IN ('sdk_headless', 'app_native')),
    capsule_json TEXT NOT NULL CHECK(length(capsule_json) > 0),
    action_json TEXT,
    route_json TEXT NOT NULL CHECK(length(route_json) > 0),
    workspace_path TEXT NOT NULL CHECK(length(workspace_path) > 1),
    result_contract_sha256 TEXT NOT NULL CHECK(length(result_contract_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('queued', 'claimed', 'starting', 'running', 'recovery_inspection_pending', 'recovery_retry_wait', 'recovery_continuation_pending', 'human_attention_required', 'result_submitted', 'finalizing', 'completed', 'failed', 'cancelled')),
    sequence INTEGER NOT NULL UNIQUE CHECK(sequence > 0),
    available_at TEXT NOT NULL,
    deadline TEXT,
    claim_epoch INTEGER,
    claim_nonce_sha256 TEXT CHECK(claim_nonce_sha256 IS NULL OR length(claim_nonce_sha256) = 64),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    thread_id TEXT,
    host_id TEXT,
    raw_result_json TEXT,
    raw_result_sha256 TEXT CHECK(raw_result_sha256 IS NULL OR length(raw_result_sha256) = 64),
    terminal_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((claim_epoch IS NULL AND claim_nonce_sha256 IS NULL) OR (claim_epoch IS NOT NULL AND claim_nonce_sha256 IS NOT NULL)),
    CHECK((raw_result_json IS NULL AND raw_result_sha256 IS NULL) OR (raw_result_json IS NOT NULL AND raw_result_sha256 IS NOT NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatches(dispatch_id) ON DELETE RESTRICT
)"""
# v10/v11 were published before recovery-state projections existed.  Keep
# their predecessor DDL exact so a real v11 ledger can migrate instead of
# being rejected merely because the current queue enum has new states.
_DISPATCH_QUEUE_PRE_RECOVERY_DDL = _DISPATCH_QUEUE_DDL.replace(
    ", 'recovery_inspection_pending', 'recovery_retry_wait', 'recovery_continuation_pending', 'human_attention_required'",
    "",
)
_ATTEMPT_CAPABILITIES_DDL = """CREATE TABLE attempt_capabilities (
    dispatch_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    operation TEXT NOT NULL CHECK(operation = 'submit_result'),
    schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256) = 64),
    workspace_path TEXT NOT NULL CHECK(length(workspace_path) > 1),
    backend TEXT NOT NULL CHECK(backend IN ('sdk_headless', 'app_native')),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    token_sha256 TEXT NOT NULL CHECK(length(token_sha256) = 64),
    accepted_result_sha256 TEXT CHECK(accepted_result_sha256 IS NULL OR length(accepted_result_sha256) = 64),
    PRIMARY KEY(dispatch_id, generation, attempt),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_SUCCESSOR_OUTBOX_DDL = """CREATE TABLE successor_outbox (
    source_dispatch_id TEXT PRIMARY KEY NOT NULL,
    successor_dispatch_id TEXT,
    payload_json TEXT NOT NULL CHECK(length(payload_json) > 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('pending', 'enqueued', 'not_required')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(source_dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE,
    FOREIGN KEY(successor_dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE RESTRICT
)"""
_NOTIFICATION_OUTBOX_DDL = """CREATE TABLE notification_outbox (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    source_task TEXT,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    outcome TEXT NOT NULL CHECK(outcome IN ('not_applicable', 'unavailable', 'attempted_ok', 'attempted_failed')),
    attempted_at TEXT,
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V10_TABLE_DDL = {
    **_V9_TABLE_DDL,
    "supervisor_authority": _SUPERVISOR_AUTHORITY_DDL,
    "dispatch_queue": _DISPATCH_QUEUE_PRE_RECOVERY_DDL,
    "attempt_capabilities": _ATTEMPT_CAPABILITIES_DDL,
    "successor_outbox": _SUCCESSOR_OUTBOX_DDL,
    "notification_outbox": _NOTIFICATION_OUTBOX_DDL,
}

# H6-E-W keeps the donor queue tables intact and adds a separately validated
# binding layer.  This makes the v10 -> v11 migration crash-atomic without
# rewriting result or cancellation rows that may already be recoverable.
_QUEUE_BINDINGS_DDL = """CREATE TABLE queue_bindings (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    plan_path TEXT NOT NULL CHECK(length(plan_path) > 0),
    plan_revision_sha256 TEXT NOT NULL CHECK(length(plan_revision_sha256) = 64),
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    source_thread_id TEXT CHECK(source_thread_id IS NULL OR length(source_thread_id) BETWEEN 1 AND 512),
    permission_mode TEXT NOT NULL CHECK(permission_mode IN ('inherit_native', 'read_only')),
    native_profile_sha256 TEXT CHECK(native_profile_sha256 IS NULL OR length(native_profile_sha256) = 64),
    native_compatibility_sha256 TEXT CHECK(native_compatibility_sha256 IS NULL OR length(native_compatibility_sha256) = 64),
    effective_permission_json TEXT,
    effective_permission_sha256 TEXT CHECK(effective_permission_sha256 IS NULL OR length(effective_permission_sha256) = 64),
    checkpoint_deadline TEXT,
    checkpoint_armed INTEGER NOT NULL CHECK(checkpoint_armed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((effective_permission_json IS NULL AND effective_permission_sha256 IS NULL)
       OR (effective_permission_json IS NOT NULL AND effective_permission_sha256 IS NOT NULL)),
    CHECK((checkpoint_deadline IS NULL AND checkpoint_armed = 0)
       OR (checkpoint_deadline IS NOT NULL AND checkpoint_armed = 1)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_WORKER_LIVENESS_DDL = """CREATE TABLE worker_liveness (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    pid INTEGER NOT NULL CHECK(pid > 0),
    process_birth_identity TEXT NOT NULL CHECK(length(process_birth_identity) > 0),
    lease_token_sha256 TEXT NOT NULL CHECK(length(lease_token_sha256) = 64),
    lease_expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    exited_at TEXT,
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_AUTHORIZED_SUCCESSORS_DDL = """CREATE TABLE authorized_successors (
    source_dispatch_id TEXT NOT NULL,
    successor_dispatch_id TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY(source_dispatch_id, successor_dispatch_id),
    CHECK(source_dispatch_id != successor_dispatch_id),
    FOREIGN KEY(source_dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE,
    FOREIGN KEY(successor_dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE RESTRICT
)"""
_WAKE_OUTBOX_DDL = """CREATE TABLE wake_outbox (
    delivery_id TEXT PRIMARY KEY NOT NULL CHECK(length(delivery_id) BETWEEN 1 AND 512),
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('checkpoint', 'terminal')),
    source_thread_id TEXT CHECK(source_thread_id IS NULL OR length(source_thread_id) BETWEEN 1 AND 512),
    payload_json TEXT NOT NULL CHECK(length(payload_json) > 0),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    state TEXT NOT NULL CHECK(state IN ('not_applicable', 'pending', 'starting', 'delivered', 'failed', 'ambiguous')),
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0 AND attempt_count <= 2),
    source_turn_id TEXT CHECK(source_turn_id IS NULL OR length(source_turn_id) BETWEEN 1 AND 512),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dispatch_id, kind),
    CHECK((source_thread_id IS NULL AND state = 'not_applicable')
       OR (source_thread_id IS NOT NULL AND state != 'not_applicable')),
    CHECK((state = 'delivered' AND source_turn_id IS NOT NULL)
       OR (state != 'delivered' AND source_turn_id IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V11_TABLE_DDL = {
    **_V10_TABLE_DDL,
    "queue_bindings": _QUEUE_BINDINGS_DDL,
    "worker_liveness": _WORKER_LIVENESS_DDL,
    "authorized_successors": _AUTHORIZED_SUCCESSORS_DDL,
    "wake_outbox": _WAKE_OUTBOX_DDL,
}

# H6-E-W-R1 recovery authority.  Recovery facts are deliberately separate
# from the queue projection: an exited process can never be mistaken for a
# queued retry, and the bounded inspection/continuation budgets survive a
# service restart.  Provider credentials are represented only by the
# profile-selected environment *name* and a profile digest; values never
# cross this table boundary.
_RECOVERY_STATE_DDL = """CREATE TABLE recovery_state (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    provider_env_key TEXT CHECK(provider_env_key IS NULL OR length(provider_env_key) BETWEEN 1 AND 256),
    profile_sha256 TEXT CHECK(profile_sha256 IS NULL OR length(profile_sha256) = 64),
    worker_exit_classification TEXT CHECK(worker_exit_classification IS NULL OR length(worker_exit_classification) BETWEEN 1 AND 128),
    exit_code INTEGER,
    recovery_state TEXT NOT NULL CHECK(recovery_state IN ('none', 'recovery_inspection_pending', 'recovery_retry_wait', 'recovery_continuation_pending', 'human_attention_required', 'completed', 'failed')),
    inspection_budget INTEGER NOT NULL CHECK(inspection_budget BETWEEN 0 AND 16),
    inspection_used INTEGER NOT NULL CHECK(inspection_used BETWEEN 0 AND 16),
    continuation_budget INTEGER NOT NULL CHECK(continuation_budget BETWEEN 0 AND 8),
    continuation_used INTEGER NOT NULL CHECK(continuation_used BETWEEN 0 AND 8),
    fresh_thread_after_idle INTEGER NOT NULL CHECK(fresh_thread_after_idle IN (0, 1)),
    fresh_thread_budget INTEGER NOT NULL CHECK(fresh_thread_budget BETWEEN 0 AND 4),
    fresh_thread_used INTEGER NOT NULL CHECK(fresh_thread_used BETWEEN 0 AND 4),
    next_eligible_at TEXT,
    inspected_thread_id TEXT CHECK(inspected_thread_id IS NULL OR length(inspected_thread_id) BETWEEN 1 AND 512),
    inspected_turn_id TEXT CHECK(inspected_turn_id IS NULL OR length(inspected_turn_id) BETWEEN 1 AND 512),
    inspected_kind TEXT CHECK(inspected_kind IS NULL OR length(inspected_kind) BETWEEN 1 AND 64),
    human_attention_reason TEXT CHECK(human_attention_reason IS NULL OR length(human_attention_reason) BETWEEN 1 AND 512),
    exited_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(inspection_used <= inspection_budget),
    CHECK(continuation_used <= continuation_budget),
    CHECK(fresh_thread_used <= fresh_thread_budget),
    CHECK((recovery_state = 'human_attention_required' AND human_attention_reason IS NOT NULL)
       OR (recovery_state != 'human_attention_required' AND human_attention_reason IS NULL)),
    CHECK((recovery_state IN ('recovery_retry_wait', 'recovery_continuation_pending') AND next_eligible_at IS NOT NULL)
       OR (recovery_state NOT IN ('recovery_retry_wait', 'recovery_continuation_pending'))),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V12_TABLE_DDL = {**_V11_TABLE_DDL, "dispatch_queue": _DISPATCH_QUEUE_DDL, "recovery_state": _RECOVERY_STATE_DDL}

# H6-F live-worker control authority. Diagnostic rows are bounded evidence
# only; retry facts and control commands are separate durable records so a
# crash cannot turn an event, command acknowledgement, or process attempt
# into terminal authority by implication.
_DIAGNOSTIC_RING_DDL = """CREATE TABLE diagnostic_ring (
    dispatch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 128),
    occurred_at TEXT NOT NULL,
    text TEXT,
    payload_sha256 TEXT CHECK(payload_sha256 IS NULL OR length(payload_sha256) = 64),
    payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0 AND payload_bytes <= 8192),
    PRIMARY KEY(dispatch_id, sequence),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_RETRY_POLICIES_DDL = """CREATE TABLE retry_policies (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    policy_version INTEGER NOT NULL CHECK(policy_version = 1),
    pre_identity_budget INTEGER NOT NULL CHECK(pre_identity_budget BETWEEN 0 AND 5),
    invalid_chain_budget INTEGER NOT NULL CHECK(invalid_chain_budget BETWEEN 0 AND 1),
    schema_envelope_budget INTEGER NOT NULL CHECK(schema_envelope_budget BETWEEN 0 AND 2),
    post_identity_loss_budget INTEGER NOT NULL CHECK(post_identity_loss_budget BETWEEN 0 AND 1),
    pre_identity_used INTEGER NOT NULL CHECK(pre_identity_used >= 0),
    invalid_chain_used INTEGER NOT NULL CHECK(invalid_chain_used >= 0),
    schema_envelope_used INTEGER NOT NULL CHECK(schema_envelope_used >= 0),
    post_identity_loss_used INTEGER NOT NULL CHECK(post_identity_loss_used >= 0),
    last_failure TEXT,
    strategy TEXT NOT NULL CHECK(strategy IN ('none', 'backoff', 'fresh_thread', 'same_thread_schema_correction', 'same_thread_continuation', 'human_attention_required')),
    next_eligible_at TEXT,
    prior_thread_id TEXT,
    prior_turn_id TEXT,
    human_attention_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(pre_identity_used <= pre_identity_budget),
    CHECK(invalid_chain_used <= invalid_chain_budget),
    CHECK(schema_envelope_used <= schema_envelope_budget),
    CHECK(post_identity_loss_used <= post_identity_loss_budget),
    CHECK((strategy = 'human_attention_required' AND human_attention_reason IS NOT NULL) OR (strategy != 'human_attention_required' AND human_attention_reason IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_CONTROL_COMMANDS_DDL = """CREATE TABLE control_commands (
    command_id TEXT PRIMARY KEY NOT NULL CHECK(length(command_id) BETWEEN 1 AND 256),
    dispatch_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 512),
    turn_id TEXT NOT NULL CHECK(length(turn_id) BETWEEN 1 AND 512),
    kind TEXT NOT NULL CHECK(kind IN ('steer', 'interrupt')),
    payload TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'acknowledged', 'rejected', 'unresolved')),
    acknowledgement_json TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    acknowledged_at TEXT,
    CHECK((kind = 'steer' AND payload IS NOT NULL AND length(payload) BETWEEN 1 AND 8192)
       OR (kind = 'interrupt' AND payload IS NULL)),
    CHECK((state = 'acknowledged' AND acknowledged_at IS NOT NULL AND acknowledgement_json IS NOT NULL)
       OR (state != 'acknowledged')),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V13_TABLE_DDL = {
    **_V12_TABLE_DDL,
    "diagnostic_ring": _DIAGNOSTIC_RING_DDL,
    "retry_policies": _RETRY_POLICIES_DDL,
    "control_commands": _CONTROL_COMMANDS_DDL,
}

# H6-F conformance repair.  Command submission order is a server-owned fact,
# and every explicit recovery action is retained as an immutable audit row.
_CONTROL_COMMANDS_V14_DDL = """CREATE TABLE control_commands (
    command_id TEXT PRIMARY KEY NOT NULL CHECK(length(command_id) BETWEEN 1 AND 256),
    dispatch_id TEXT NOT NULL,
    submission_sequence INTEGER NOT NULL CHECK(submission_sequence > 0),
    generation INTEGER NOT NULL CHECK(generation > 0),
    attempt INTEGER NOT NULL CHECK(attempt > 0),
    thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 512),
    turn_id TEXT NOT NULL CHECK(length(turn_id) BETWEEN 1 AND 512),
    kind TEXT NOT NULL CHECK(kind IN ('steer', 'interrupt')),
    payload TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'acknowledged', 'rejected', 'unresolved')),
    acknowledgement_json TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    acknowledged_at TEXT,
    UNIQUE(dispatch_id, submission_sequence),
    CHECK((kind = 'steer' AND payload IS NOT NULL AND length(payload) BETWEEN 1 AND 8192)
       OR (kind = 'interrupt' AND payload IS NULL)),
    CHECK((state = 'acknowledged' AND acknowledged_at IS NOT NULL AND acknowledgement_json IS NOT NULL)
       OR (state != 'acknowledged')),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_RETRY_POLICIES_V14_DDL = """CREATE TABLE retry_policies (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    policy_version INTEGER NOT NULL CHECK(policy_version = 1),
    pre_identity_budget INTEGER NOT NULL CHECK(pre_identity_budget BETWEEN 0 AND 5),
    invalid_chain_budget INTEGER NOT NULL CHECK(invalid_chain_budget BETWEEN 0 AND 1),
    schema_envelope_budget INTEGER NOT NULL CHECK(schema_envelope_budget BETWEEN 0 AND 2),
    post_identity_loss_budget INTEGER NOT NULL CHECK(post_identity_loss_budget BETWEEN 0 AND 1),
    pre_identity_used INTEGER NOT NULL CHECK(pre_identity_used >= 0),
    invalid_chain_used INTEGER NOT NULL CHECK(invalid_chain_used >= 0),
    schema_envelope_used INTEGER NOT NULL CHECK(schema_envelope_used >= 0),
    post_identity_loss_used INTEGER NOT NULL CHECK(post_identity_loss_used >= 0),
    last_failure TEXT,
    strategy TEXT NOT NULL CHECK(strategy IN ('none', 'backoff', 'fresh_thread', 'same_thread_schema_correction', 'same_thread_continuation', 'human_attention_required')),
    next_eligible_at TEXT,
    prior_thread_id TEXT,
    prior_turn_id TEXT,
    human_attention_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(pre_identity_used <= pre_identity_budget),
    CHECK(invalid_chain_used <= invalid_chain_budget),
    CHECK(schema_envelope_used <= schema_envelope_budget),
    CHECK(post_identity_loss_used <= post_identity_loss_budget),
    CHECK((strategy = 'human_attention_required' AND human_attention_reason IS NOT NULL) OR (strategy != 'human_attention_required' AND human_attention_reason IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_RECOVERY_CONTROLS_DDL = """CREATE TABLE recovery_controls (
    action_id TEXT PRIMARY KEY NOT NULL CHECK(length(action_id) BETWEEN 1 AND 256),
    dispatch_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    action_kind TEXT NOT NULL CHECK(action_kind IN ('retry', 'cancel', 'budget_change')),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512),
    requested_budget_json TEXT,
    applied_revision INTEGER NOT NULL CHECK(applied_revision >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V14_TABLE_DDL = {
    **_V13_TABLE_DDL,
    "retry_policies": _RETRY_POLICIES_V14_DDL,
    "control_commands": _CONTROL_COMMANDS_V14_DDL,
    "recovery_controls": _RECOVERY_CONTROLS_DDL,
}

# H6-G durable controller-decision authority.  These tables deliberately sit
# beside (rather than inside) the worker queue: wake delivery, model/human
# claims, model generations, action effects and acknowledgement are distinct
# crash-atomic facts.
_CONTROLLER_DECISIONS_DDL = """CREATE TABLE controller_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL CHECK(length(decision_id) BETWEEN 10 AND 512),
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('checkpoint', 'terminal')),
    cycle_sequence INTEGER NOT NULL CHECK(cycle_sequence BETWEEN 1 AND 64),
    summary_json TEXT NOT NULL CHECK(length(summary_json) BETWEEN 2 AND 16384),
    summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256) = 64),
    source_thread_id TEXT CHECK(source_thread_id IS NULL OR length(source_thread_id) BETWEEN 1 AND 512),
    state TEXT NOT NULL CHECK(state IN ('pending_delivery', 'awaiting_claim', 'claimed', 'action_committed', 'acknowledged', 'superseded', 'human_attention_required', 'legacy_closed')),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    current_generation INTEGER NOT NULL CHECK(current_generation BETWEEN 1 AND 2),
    generation_budget INTEGER NOT NULL CHECK(generation_budget BETWEEN 1 AND 2),
    generation_used INTEGER NOT NULL CHECK(generation_used BETWEEN 0 AND 2),
    claimant_kind TEXT CHECK(claimant_kind IS NULL OR claimant_kind IN ('model', 'human')),
    claimant_id TEXT CHECK(claimant_id IS NULL OR length(claimant_id) BETWEEN 1 AND 256),
    claim_token_sha256 TEXT CHECK(claim_token_sha256 IS NULL OR length(claim_token_sha256) = 64),
    claim_started_at TEXT,
    claim_lease_expires_at TEXT,
    action_id TEXT,
    action_bundle_json TEXT,
    action_bundle_sha256 TEXT CHECK(action_bundle_sha256 IS NULL OR length(action_bundle_sha256) = 64),
    committed_at TEXT,
    acknowledged_at TEXT,
    superseded_at TEXT,
    deadline TEXT NOT NULL CHECK(length(deadline) BETWEEN 1 AND 64),
    human_attention_reason TEXT CHECK(human_attention_reason IS NULL OR length(human_attention_reason) BETWEEN 1 AND 512),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dispatch_id, kind, cycle_sequence),
    CHECK((claimant_kind IS NULL AND claimant_id IS NULL AND claim_token_sha256 IS NULL AND claim_started_at IS NULL)
       OR (claimant_kind IS NOT NULL AND claimant_id IS NOT NULL AND claim_token_sha256 IS NOT NULL AND claim_started_at IS NOT NULL)),
    CHECK((action_id IS NULL AND action_bundle_json IS NULL AND action_bundle_sha256 IS NULL AND committed_at IS NULL)
       OR (action_id IS NOT NULL AND action_bundle_json IS NOT NULL AND action_bundle_sha256 IS NOT NULL AND committed_at IS NOT NULL)),
    CHECK((state = 'acknowledged' AND acknowledged_at IS NOT NULL) OR (state != 'acknowledged')),
    CHECK((state = 'human_attention_required' AND human_attention_reason IS NOT NULL) OR (state != 'human_attention_required')),
    CHECK(generation_used <= generation_budget),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_CONTROLLER_GENERATIONS_DDL = """CREATE TABLE controller_decision_generations (
    decision_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2),
    lineage_id TEXT NOT NULL CHECK(length(lineage_id) BETWEEN 1 AND 256),
    predecessor_generation INTEGER CHECK(predecessor_generation IS NULL OR predecessor_generation BETWEEN 1 AND 2),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('source_controller', 'replacement_controller')),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'delivery_starting', 'active', 'completed', 'failed', 'interrupted', 'ambiguous', 'unavailable', 'superseded')),
    prompt_sha256 TEXT CHECK(prompt_sha256 IS NULL OR length(prompt_sha256) = 64),
    controller_thread_id TEXT CHECK(controller_thread_id IS NULL OR length(controller_thread_id) BETWEEN 1 AND 512),
    controller_turn_id TEXT CHECK(controller_turn_id IS NULL OR length(controller_turn_id) BETWEEN 1 AND 512),
    inspection_started_at TEXT,
    inspection_token_sha256 TEXT CHECK(inspection_token_sha256 IS NULL OR length(inspection_token_sha256) = 64),
    inspection_lease_expires_at TEXT,
    inspection_completed_at TEXT,
    inspection_outcome TEXT,
    inspection_bundle_json TEXT CHECK(inspection_bundle_json IS NULL OR length(inspection_bundle_json) BETWEEN 2 AND 32768),
    inspection_bundle_sha256 TEXT CHECK(inspection_bundle_sha256 IS NULL OR length(inspection_bundle_sha256) = 64),
    started_at TEXT,
    terminal_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(decision_id, generation),
    UNIQUE(controller_thread_id, controller_turn_id),
    CHECK(controller_thread_id IS NOT NULL OR controller_turn_id IS NULL),
    CHECK(inspection_completed_at IS NULL OR inspection_started_at IS NOT NULL),
    CHECK(inspection_token_sha256 IS NULL OR inspection_started_at IS NOT NULL),
    CHECK(inspection_lease_expires_at IS NULL OR inspection_token_sha256 IS NOT NULL),
    CHECK((inspection_bundle_json IS NULL AND inspection_bundle_sha256 IS NULL)
       OR (inspection_bundle_json IS NOT NULL AND inspection_bundle_sha256 IS NOT NULL)),
    FOREIGN KEY(decision_id) REFERENCES controller_decisions(decision_id) ON DELETE CASCADE
)"""
_CONTROLLER_ACTION_OUTBOX_DDL = """CREATE TABLE controller_action_outbox (
    action_id TEXT PRIMARY KEY NOT NULL CHECK(length(action_id) BETWEEN 1 AND 256),
    decision_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2),
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    claimant_kind TEXT NOT NULL CHECK(claimant_kind IN ('model', 'human')),
    claimant_id TEXT NOT NULL CHECK(length(claimant_id) BETWEEN 1 AND 256),
    bundle_json TEXT NOT NULL CHECK(length(bundle_json) BETWEEN 2 AND 32768),
    bundle_sha256 TEXT NOT NULL CHECK(length(bundle_sha256) = 64),
    effect_receipt_json TEXT NOT NULL CHECK(length(effect_receipt_json) BETWEEN 2 AND 32768),
    effect_receipt_sha256 TEXT NOT NULL CHECK(length(effect_receipt_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('committed', 'acknowledged')),
    committed_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE(decision_id),
    UNIQUE(decision_id, bundle_sha256),
    FOREIGN KEY(decision_id) REFERENCES controller_decisions(decision_id) ON DELETE CASCADE
)"""
_WAKE_OUTBOX_V15_DDL = """CREATE TABLE wake_outbox (
    delivery_id TEXT PRIMARY KEY NOT NULL CHECK(length(delivery_id) BETWEEN 1 AND 512),
    dispatch_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('checkpoint', 'terminal')),
    cycle_sequence INTEGER NOT NULL CHECK(cycle_sequence BETWEEN 1 AND 64),
    source_thread_id TEXT CHECK(source_thread_id IS NULL OR length(source_thread_id) BETWEEN 1 AND 512),
    payload_json TEXT NOT NULL CHECK(length(payload_json) > 0),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    state TEXT NOT NULL CHECK(state IN ('not_applicable', 'pending', 'starting', 'delivered', 'failed', 'ambiguous', 'suppressed')),
    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0 AND attempt_count <= 2),
    source_turn_id TEXT CHECK(source_turn_id IS NULL OR length(source_turn_id) BETWEEN 1 AND 512),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dispatch_id, kind, cycle_sequence),
    UNIQUE(decision_id),
    CHECK((source_thread_id IS NULL AND state IN ('not_applicable', 'suppressed')) OR (source_thread_id IS NOT NULL AND state != 'not_applicable')),
    CHECK((state = 'delivered' AND source_turn_id IS NOT NULL) OR (state != 'delivered' AND source_turn_id IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE,
    FOREIGN KEY(decision_id) REFERENCES controller_decisions(decision_id) ON DELETE CASCADE
)"""
_V15_TABLE_DDL = {
    **_V14_TABLE_DDL,
    "controller_decisions": _CONTROLLER_DECISIONS_DDL,
    "controller_decision_generations": _CONTROLLER_GENERATIONS_DDL,
    "controller_action_outbox": _CONTROLLER_ACTION_OUTBOX_DDL,
    "wake_outbox": _WAKE_OUTBOX_V15_DDL,
}
_V15_INDEX_DDL = {
    "controller_decisions_due_idx": (
        "controller_decisions",
        "CREATE INDEX controller_decisions_due_idx ON controller_decisions(state, deadline, decision_id)",
    ),
    "controller_decision_generations_state_idx": (
        "controller_decision_generations",
        "CREATE INDEX controller_decision_generations_state_idx ON controller_decision_generations(state, decision_id, generation)",
    ),
    "controller_action_outbox_committed_idx": (
        "controller_action_outbox",
        "CREATE INDEX controller_action_outbox_committed_idx ON controller_action_outbox(state, committed_at, action_id)",
    ),
}

_RECOVERY_CONTROLS_V16_DDL = """CREATE TABLE recovery_controls (
    action_id TEXT PRIMARY KEY NOT NULL CHECK(length(action_id) BETWEEN 1 AND 256),
    dispatch_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
    action_kind TEXT NOT NULL CHECK(action_kind IN ('retry', 'cancel', 'budget_change', 'compatibility_rebind')),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512),
    requested_budget_json TEXT,
    compatibility_rebind_json TEXT,
    applied_revision INTEGER NOT NULL CHECK(applied_revision >= 0),
    created_at TEXT NOT NULL,
    CHECK((action_kind = 'budget_change' AND requested_budget_json IS NOT NULL)
       OR (action_kind != 'budget_change' AND requested_budget_json IS NULL)),
    CHECK((action_kind = 'compatibility_rebind' AND compatibility_rebind_json IS NOT NULL)
       OR (action_kind != 'compatibility_rebind' AND compatibility_rebind_json IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V16_TABLE_DDL = {
    **_V15_TABLE_DDL,
    "recovery_controls": _RECOVERY_CONTROLS_V16_DDL,
}

# H6-E-W-R2 provider-transient recovery.  The existing retry-policy table is
# the sole authority: three default same-thread continuations are tracked
# separately from result-loss recovery, and one explicit control action may
# authorize exactly one additional continuation without resetting usage.
_RETRY_POLICIES_V17_DDL = """CREATE TABLE retry_policies (
    dispatch_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    policy_version INTEGER NOT NULL CHECK(policy_version = 1),
    pre_identity_budget INTEGER NOT NULL CHECK(pre_identity_budget BETWEEN 0 AND 5),
    invalid_chain_budget INTEGER NOT NULL CHECK(invalid_chain_budget BETWEEN 0 AND 1),
    schema_envelope_budget INTEGER NOT NULL CHECK(schema_envelope_budget BETWEEN 0 AND 2),
    post_identity_loss_budget INTEGER NOT NULL CHECK(post_identity_loss_budget BETWEEN 0 AND 1),
    pre_identity_used INTEGER NOT NULL CHECK(pre_identity_used >= 0),
    invalid_chain_used INTEGER NOT NULL CHECK(invalid_chain_used >= 0),
    schema_envelope_used INTEGER NOT NULL CHECK(schema_envelope_used >= 0),
    post_identity_loss_used INTEGER NOT NULL CHECK(post_identity_loss_used >= 0),
    last_failure TEXT,
    strategy TEXT NOT NULL CHECK(strategy IN ('none', 'backoff', 'fresh_thread', 'same_thread_schema_correction', 'same_thread_continuation', 'human_attention_required')),
    next_eligible_at TEXT,
    prior_thread_id TEXT,
    prior_turn_id TEXT,
    human_attention_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider_transient_budget INTEGER NOT NULL DEFAULT 3 CHECK(provider_transient_budget BETWEEN 0 AND 4),
    provider_transient_used INTEGER NOT NULL DEFAULT 0 CHECK(provider_transient_used >= 0),
    provider_transient_grant_used INTEGER NOT NULL DEFAULT 0 CHECK(provider_transient_grant_used IN (0, 1)),
    CHECK(pre_identity_used <= pre_identity_budget),
    CHECK(invalid_chain_used <= invalid_chain_budget),
    CHECK(schema_envelope_used <= schema_envelope_budget),
    CHECK(post_identity_loss_used <= post_identity_loss_budget),
    CHECK(provider_transient_used <= provider_transient_budget),
    CHECK(provider_transient_budget <= 3 + provider_transient_grant_used),
    CHECK((strategy = 'human_attention_required' AND human_attention_reason IS NOT NULL) OR (strategy != 'human_attention_required' AND human_attention_reason IS NULL)),
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE
)"""
_V17_TABLE_DDL = {
    **_V16_TABLE_DDL,
    "retry_policies": _RETRY_POLICIES_V17_DDL,
}

# H6-E-W-R3 program authority.  Program identity and revision live on the
# existing run authority; only dependency edges and the deterministic Git
# effect outbox are new durable tables.  Controller decisions keep their
# existing dispatch anchor for backwards compatibility while carrying an
# explicit program event identity.
_PROGRAM_RUNS_DDL = """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL CHECK(length(run_id) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(metadata_json = '{}'),
    program_digest TEXT CHECK(program_digest IS NULL OR length(program_digest) = 64),
    program_revision INTEGER NOT NULL DEFAULT 0 CHECK(program_revision >= 0),
    program_state TEXT CHECK(program_state IS NULL OR program_state IN ('registered', 'running', 'completed', 'needs_decision', 'external_blocked', 'failed')),
    program_graph_json TEXT,
    trunk_head TEXT CHECK(trunk_head IS NULL OR length(trunk_head) = 40),
    CHECK((program_digest IS NULL AND program_state IS NULL AND program_graph_json IS NULL AND trunk_head IS NULL AND program_revision = 0)
       OR (program_digest IS NOT NULL AND program_state IS NOT NULL AND program_graph_json IS NOT NULL AND trunk_head IS NOT NULL))
)"""
_PROGRAM_CONTROLLER_DECISIONS_DDL = (
    _CONTROLLER_DECISIONS_DDL.replace(
        "    dispatch_id TEXT NOT NULL,\n",
        "    dispatch_id TEXT,\n    program_id TEXT,\n    program_revision INTEGER CHECK(program_revision IS NULL OR program_revision >= 0),\n    event_kind TEXT CHECK(event_kind IS NULL OR event_kind IN ('implementation_completed', 'review_completed', 'integration_completed', 'controller_attention', 'checkpoint')),\n    event_key TEXT CHECK(event_key IS NULL OR length(event_key) BETWEEN 1 AND 256),\n",
        1,
    )
    .replace(
        "    UNIQUE(dispatch_id, kind, cycle_sequence),",
        "    UNIQUE(dispatch_id, kind, cycle_sequence),\n    UNIQUE(program_id, event_key),",
    )
    .replace(
        "    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE\n)",
        "    CHECK((program_id IS NULL AND program_revision IS NULL AND event_kind IS NULL AND event_key IS NULL)\n       OR (program_id IS NOT NULL AND program_revision IS NOT NULL AND event_kind IS NOT NULL AND event_key IS NOT NULL)),\n    FOREIGN KEY(dispatch_id) REFERENCES dispatch_queue(dispatch_id) ON DELETE CASCADE\n)",
    )
)
_PROGRAM_DISPATCHES_DDL = _DISPATCHES_DDL.replace(
    "    UNIQUE(run_id, milestone_id, role),",
    "    UNIQUE(run_id, milestone_id, role, generation),",
)
_MILESTONE_DEPENDENCIES_DDL = """CREATE TABLE milestone_dependencies (
    program_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    dependency_milestone_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(program_id, milestone_id, dependency_milestone_id),
    CHECK(milestone_id != dependency_milestone_id),
    FOREIGN KEY(program_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE,
    FOREIGN KEY(program_id, dependency_milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE
)"""
_INTEGRATION_OUTBOX_DDL = """CREATE TABLE integration_outbox (
    integration_id TEXT PRIMARY KEY NOT NULL CHECK(length(integration_id) BETWEEN 1 AND 512),
    program_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    candidate_sha TEXT NOT NULL CHECK(length(candidate_sha) = 40),
    expected_trunk_head TEXT NOT NULL CHECK(length(expected_trunk_head) = 40),
    strategy TEXT NOT NULL CHECK(strategy IN ('merge', 'fast_forward', 'cherry_pick')),
    state TEXT NOT NULL CHECK(state IN ('pending', 'applied', 'conflict', 'failed')),
    receipt_json TEXT,
    receipt_sha256 TEXT CHECK(receipt_sha256 IS NULL OR length(receipt_sha256) = 64),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(program_id, milestone_id, candidate_sha),
    FOREIGN KEY(program_id, milestone_id) REFERENCES milestones(run_id, milestone_id) ON DELETE CASCADE,
    CHECK((receipt_json IS NULL AND receipt_sha256 IS NULL) OR (receipt_json IS NOT NULL AND receipt_sha256 IS NOT NULL)),
    CHECK((state = 'pending' AND completed_at IS NULL) OR (state IN ('applied', 'conflict', 'failed') AND completed_at IS NOT NULL))
)"""
_V18_TABLE_DDL = {
    **_V17_TABLE_DDL,
    "dispatches": _PROGRAM_DISPATCHES_DDL,
    "runs": _PROGRAM_RUNS_DDL,
    "controller_decisions": _PROGRAM_CONTROLLER_DECISIONS_DDL,
    "milestone_dependencies": _MILESTONE_DEPENDENCIES_DDL,
    "integration_outbox": _INTEGRATION_OUTBOX_DDL,
}

# H6-E-R4 forward-only terminology cutover.  Historical v10-v18 DDL remains
# immutable provenance; the live authority is recreated under its semantic
# harness name by one atomic migration.
_HARNESS_AUTHORITY_DDL = _SUPERVISOR_AUTHORITY_DDL.replace(
    "CREATE TABLE supervisor_authority", "CREATE TABLE harness_authority", 1
)
_V19_TABLE_DDL = {
    **{name: statement for name, statement in _V18_TABLE_DDL.items() if name != "supervisor_authority"},
    "harness_authority": _HARNESS_AUTHORITY_DDL,
}


def _canonical_ddl(sql: str) -> str:
    """Return exact SQLite DDL modulo whitespace outside quoted values."""

    canonical: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote is None:
            if character.isspace():
                index += 1
                continue
            canonical.append(character)
            if character in {"'", '"'}:
                quote = character
        else:
            canonical.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    canonical.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        index += 1
    normalized = "".join(canonical)
    # SQLite serializes tables rebuilt with ``ALTER TABLE ... RENAME`` using
    # optional identifier quotes.  Treat that representation as equivalent
    # to the controller-owned DDL while keeping quoted string literals intact.
    return re.sub(r'^CREATETABLE"([A-Za-z_][A-Za-z0-9_]*)"', r"CREATETABLE\1", normalized)


def _process_birth_identity(pid: int) -> str:
    """Return the Linux process-birth identity used by harness leases."""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return fields[21]
    except (OSError, IndexError, ValueError):
        return f"pid:{pid}"


SchemaObject = tuple[str, str, str, str | None]
FileIdentity = tuple[int, int]


def _owned_inventory(definitions: Mapping[str, str]) -> tuple[SchemaObject, ...]:
    return tuple(sorted(("table", name, name, sql) for name, sql in definitions.items()))


def _canonical_inventory(inventory: tuple[SchemaObject, ...]) -> tuple[SchemaObject, ...]:
    return tuple(
        (object_type, name, table, _canonical_ddl(sql) if sql is not None else None)
        for object_type, name, table, sql in inventory
    )


def _inventory_fingerprint(inventory: tuple[SchemaObject, ...]) -> str:
    payload = "\n".join(repr(item) for item in inventory)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _schema_inventory(connection: sqlite3.Connection, *, temporary: bool = False) -> tuple[SchemaObject, ...]:
    statement = (
        "SELECT type, name, tbl_name, sql FROM sqlite_temp_master "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
        if temporary
        else "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
    )
    rows = connection.execute(statement).fetchall()
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]) if row[3] is not None else None,
        )
        for row in rows
    )


_SCHEMA_IDENTITIES = {
    SchemaVersion(1): "codex_flow_h2_v1",
    SchemaVersion(2): "codex_flow_h2_v2",
    SchemaVersion(3): "codex_flow_h3_v3",
    SchemaVersion(4): "codex_flow_h3_integrity_v4",
    SchemaVersion(5): "codex_flow_h3_native_profile_v5",
    SchemaVersion(6): "codex_flow_h3_permission_authority_v6",
    SchemaVersion(7): "codex_flow_h3_causal_workspace_v7",
    SchemaVersion(8): "codex_flow_h3_terminal_workspace_v8",
    SchemaVersion(9): "codex_flow_h6_app_native_v9",
    SchemaVersion(10): "codex_flow_h6e_detached_supervisor_v10",
    SchemaVersion(11): "codex_flow_h6e_harness_wake_v11",
    SchemaVersion(12): "codex_flow_h6e_harness_recovery_v12",
    SchemaVersion(13): "codex_flow_h6e_live_control_v13",
    SchemaVersion(14): "codex_flow_h6f_live_control_conformance_v14",
    SchemaVersion(15): "codex_flow_controller_decision_recovery_v15",
    SchemaVersion(16): "codex_flow_compatibility_rebind_recovery_v16",
    SchemaVersion(17): "codex_flow_provider_transient_recovery_v17",
    SchemaVersion(18): "codex_flow_event_driven_program_controller_v18",
    SchemaVersion(19): "codex_flow_harness_candidate_retention_v19",
}
_SCHEMA_IDENTITY = _SCHEMA_IDENTITIES[CURRENT_SCHEMA_VERSION]


def ledger_schema_compatibility(path: Path) -> JsonObject:
    """Inspect migration compatibility without creating or migrating a ledger."""

    if not path.exists():
        return {
            "candidate_schema_version": int(CURRENT_SCHEMA_VERSION),
            "ledger_schema_version": None,
            "compatible": True,
            "migration_required": False,
        }
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise CorruptSchemaError("unable to inspect ledger schema read-only") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if row is None:
        raise CorruptSchemaError("schema_version marker is missing")
    try:
        version = SchemaVersion(row[0])
    except ValueError as exc:
        raise CorruptSchemaError("schema_version marker is not an integer") from exc
    draft_upgrade_required = False
    if version == CURRENT_SCHEMA_VERSION:
        ddl_connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            raw_ddl = ddl_connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'app_native_dispatches'"
            ).fetchone()
            count = int(ddl_connection.execute("SELECT COUNT(*) FROM app_native_dispatches").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise CorruptSchemaError("unable to inspect App-native schema read-only") from exc
        finally:
            ddl_connection.close()
        draft_upgrade_required = raw_ddl is not None and _canonical_ddl(str(raw_ddl[0])) == _canonical_ddl(
            _APP_NATIVE_DISPATCHES_DRAFT_V9_DDL
        )
        if draft_upgrade_required and count != 0:
            return {
                "candidate_schema_version": int(CURRENT_SCHEMA_VERSION),
                "ledger_schema_version": int(version),
                "compatible": False,
                "migration_required": True,
            }
    return {
        "candidate_schema_version": int(CURRENT_SCHEMA_VERSION),
        "ledger_schema_version": int(version),
        "compatible": version in SUPPORTED_SCHEMA_VERSIONS and version <= CURRENT_SCHEMA_VERSION,
        "migration_required": version < CURRENT_SCHEMA_VERSION or draft_upgrade_required,
    }


class LedgerError(RuntimeError):
    """Base class for durable-ledger failures."""


class LedgerClosedError(LedgerError):
    """An operation was attempted after ``close``."""


class SchemaError(LedgerError):
    """The SQLite file is corrupt or is not an owned ledger schema."""


class CorruptSchemaError(SchemaError):
    """Schema metadata or required tables/columns are invalid."""


class UnsupportedSchemaVersion(SchemaError):
    """The ledger was written by a newer or unsupported schema."""


class MigrationRequired(SchemaError):
    """An older ledger requires an explicit, fenced migration authority."""


class RecordNotFound(LedgerError):
    """A requested run, milestone, or dispatch does not exist."""


class InvalidTransition(LedgerError):
    """A state transition is not in the explicit H2 transition table."""


class StaleWriter(LedgerError):
    """The caller's expected predecessor is no longer current."""


class HarnessRefreshBlocked(LedgerError):
    """A refresh fence or active child prevents a new lifecycle action."""


class NativeCompatibilityConflict(LedgerError):
    """Same-thread resume is unsafe under changed provider/discovery facts."""


class NativePermissionConflict(LedgerError):
    """Native permission authorities cannot be ordered monotonically."""


class DispatchConflict(LedgerError):
    """A milestone/role is already owned by another logical dispatch."""


class WorkspaceLeaseConflict(LedgerError):
    """A workspace or execution owner conflicts with durable lease facts."""


FaultInjector = Callable[[str], None]


def utc_now() -> str:
    """Return a sortable, explicit UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _default_executor_blocker(terminal_status: str) -> TypedBlocker:
    """Project a missing terminal blocker without losing its lifecycle scope."""

    if terminal_status == "external_blocked":
        return TypedBlocker(
            gate_id="executor-terminal-facts",
            kind=BlockerKind.EXTERNAL,
            scope=BlockerScope.CURRENT_PROMOTION,
            promotion_blocking=True,
            required_action="Resolve the external prerequisite before promoting the retained candidate.",
        )
    if terminal_status == "needs_decision":
        return TypedBlocker(
            gate_id="executor-terminal-facts",
            kind=BlockerKind.DECISION,
            scope=BlockerScope.CURRENT_REPAIR,
            promotion_blocking=True,
            required_action="Resolve the controller decision before promoting or repairing the retained candidate.",
        )
    return TypedBlocker(
        gate_id="executor-terminal-facts",
        kind=BlockerKind.EXECUTION,
        scope=BlockerScope.CURRENT_REPAIR,
        promotion_blocking=True,
        required_action="Inspect the retained terminal workspace and choose repair or abandonment.",
    )


def _normalize_terminal_blocker(terminal_status: str, blocker: TypedBlocker | None) -> TypedBlocker | None:
    """Keep terminal outcomes promotion-blocking at the harness boundary.

    Provider/model blocker facts are advisory input.  A non-completed terminal
    result always retains a current-candidate blocker; only an already stronger
    harness-authored integrity blocker may pass through unchanged.
    """

    if terminal_status == "completed":
        return blocker
    if blocker is None:
        return _default_executor_blocker(terminal_status)
    if blocker.gate_id == "candidate-integrity" and blocker.promotion_blocking:
        return blocker
    scope = BlockerScope.CURRENT_PROMOTION if terminal_status == "external_blocked" else BlockerScope.CURRENT_REPAIR
    if blocker.promotion_blocking and blocker.scope is scope:
        return blocker
    return TypedBlocker(
        blocker.gate_id,
        blocker.kind,
        scope,
        True,
        blocker.required_action,
    )


def _identifier(value: str, constructor: Callable[[str], Any], label: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: expected a string, got {type(value).__name__}")
    try:
        return constructor(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc


def _run_id(value: RunId | str) -> RunId:
    return _identifier(value, RunId, "run id")


def _milestone_id(value: MilestoneId | str) -> MilestoneId:
    return _identifier(value, MilestoneId, "milestone id")


def _role(value: RoleId | str) -> RoleId:
    return _identifier(value, RoleId, "role")


def _generation(value: Generation | int | str) -> Generation:
    try:
        return Generation(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid generation: {value!r}") from exc


def _empty_object(value: Mapping[str, object] | None, *, field_name: str) -> tuple[JsonObject, str]:
    """Reject generic durable content; H2 persists identifiers and facts only."""

    if value is not None and not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if value is not None and dict(value):
        raise ValueError(f"{field_name} does not accept arbitrary durable fields")
    return {}, "{}"


def _decode_object(raw: str | None, *, field_name: str) -> JsonObject:
    if raw in (None, "{}"):
        return {}
    raise SchemaError(f"{field_name} contains unsupported durable content")


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode_json_object(raw: str | None, *, field_name: str) -> JsonObject | None:
    if raw is None:
        return None
    try:
        decoded = strict_json_loads(raw)
    except ValueError as exc:
        raise SchemaError(f"{field_name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SchemaError(f"{field_name} must be a JSON object")
    return decoded


def _encode_h4_event_data(value: Mapping[str, object]) -> tuple[JsonObject, str]:
    """Encode the one explicitly owned non-empty event envelope."""

    if not isinstance(value, Mapping):
        raise TypeError("H4 event data must be a mapping")
    try:
        decoded = strict_json_loads(_encode_json(dict(value)))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("H4 event data must be interoperable JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("H4 event data must be a JSON object")
    envelope: JsonObject = {"__h4": decoded}
    return envelope, _encode_json(envelope)


def _decode_event_data(raw: str | None) -> JsonObject | None:
    if raw is None:
        return None
    decoded = _decode_json_object(raw, field_name="event data")
    if decoded is None or set(decoded) != {"__h4"} or not isinstance(decoded["__h4"], dict):
        raise SchemaError("event data contains unsupported durable content")
    return decoded


def _sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _command_id(value: object) -> str:
    """Validate a command identity before any conversion or durable lookup."""

    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 256 or "\x00" in value:
        raise ValueError("control command id is invalid")
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise ValueError("control command id is invalid")
    return value


def _git_sha(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError(f"{field_name} must be a lowercase 40-character Git SHA")
    return value


def _encode_workspace_baseline(entries: tuple[tuple[str, str], ...]) -> tuple[str, str]:
    normalized: list[list[str]] = []
    prior = ""
    for path, signature in entries:
        candidate = Path(path)
        if (
            not path
            or path <= prior
            or path == "."
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() != path
        ):
            raise ValueError("workspace baseline paths must be sorted unique repository-relative paths")
        if signature != "missing" and re.fullmatch(r"(?:file|directory):[0-9a-f]{64}", signature) is None:
            raise ValueError("workspace baseline signatures must be typed SHA-256 facts")
        normalized.append([path, signature])
        prior = path
    encoded = _encode_json(normalized)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_workspace_baseline(raw: str, digest: str) -> tuple[tuple[str, str], ...]:
    try:
        decoded = strict_json_loads(raw)
    except ValueError as exc:
        raise SchemaError("workspace baseline is not valid JSON") from exc
    if not isinstance(decoded, list) or any(
        not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], str)
        for item in decoded
    ):
        raise SchemaError("workspace baseline must be a list of path/signature pairs")
    entries = tuple((item[0], item[1]) for item in decoded)
    try:
        canonical, actual = _encode_workspace_baseline(entries)
    except ValueError as exc:
        raise SchemaError("workspace baseline contains invalid facts") from exc
    if canonical != raw or actual != digest:
        raise CorruptSchemaError("workspace baseline digest does not match its facts")
    return entries


def _reason(value: WorkflowReason | ReasonCode | str | BaseException | None) -> WorkflowReason | None:
    if value is None:
        return None
    if isinstance(value, WorkflowReason):
        if value.detail is not None:
            raise ValueError("durable workflow reasons do not carry free-form detail")
        return value
    if isinstance(value, TransportFailureBeforeIdentity):
        return PreIdentityTransportFailure()
    if isinstance(value, TerminalFailureAfterIdentity):
        return PostIdentityExecutionFailure()
    if isinstance(value, ReasonCode):
        return WorkflowReason(value)
    try:
        return WorkflowReason(ReasonCode(value))
    except ValueError as exc:
        raise ValueError(f"unknown reason code: {value!r}") from exc


def _row_state(value: str) -> WorkflowState:
    try:
        return WorkflowState(value)
    except ValueError as exc:
        raise SchemaError(f"unknown persisted workflow state: {value!r}") from exc


def _event_type(value: str) -> str:
    if value not in _EVENT_TYPES:
        raise ValueError(f"unsupported event type: {value!r}")
    return value


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _canonical_workspace_path(path: str | Path) -> Path:
    return Path(path).resolve(strict=False)


def _persisted_canonical_path(value: object, *, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise CorruptSchemaError(f"persisted {label} is not a canonical physical path")
    return path


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _open_parent_chain(path: Path) -> tuple[int, ...]:
    """Create and pin every parent using no-follow directory descriptors."""

    descriptors = [os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for component in path.parent.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            descriptors.append(next_fd)
        return tuple(descriptors)
    except (NotADirectoryError, OSError) as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise SchemaError(f"ledger ancestor is not a stable real directory: {path.parent}") from exc


class Ledger:
    """One connection to one repository-local workflow ledger."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        fault_injector: FaultInjector | None = None,
        migrate: bool = False,
        allow_legacy: bool = False,
    ) -> None:
        if timeout <= 0:
            raise ValueError("SQLite busy timeout must be positive")
        self.path = _absolute_path(path)
        self._timeout = timeout
        self._fault_injector = fault_injector
        self._migrate_requested = migrate
        self._allow_legacy = allow_legacy
        self._legacy_schema_version: SchemaVersion | None = None
        self._connection: sqlite3.Connection | None = None
        self._database_fd: int | None = None
        self._directory_fds: tuple[int, ...] = ()
        self._open_identity: FileIdentity | None = None
        self._expected_identity: FileIdentity | None = None
        self._open()

    def _open(self) -> None:
        if self._connection is not None:
            return
        directory_fds: tuple[int, ...] = ()
        database_fd: int | None = None
        connection: sqlite3.Connection | None = None
        created = False
        restore_fenced_v18_on_failure = False

        def restore_failed_migration() -> None:
            if not restore_fenced_v18_on_failure or self._connection is None:
                return
            try:
                self._restore_fenced_v18_after_failed_open()
            except BaseException as rollback_error:
                self._close_connection()
                self.close()
                raise CorruptSchemaError(
                    "failed v18 migration opener could not restore the fenced predecessor schema"
                ) from rollback_error

        try:
            directory_fds = _open_parent_chain(self.path)
            try:
                database_fd = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=directory_fds[-1],
                )
            except FileNotFoundError as exc:
                if self._expected_identity is not None:
                    raise SchemaError("bound ledger path is missing") from exc
                try:
                    database_fd = os.open(
                        self.path.name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fds[-1],
                    )
                    created = True
                except FileExistsError:
                    database_fd = os.open(
                        self.path.name,
                        os.O_RDWR | os.O_NOFOLLOW,
                        dir_fd=directory_fds[-1],
                    )
            pinned = os.fstat(database_fd)
            if not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink != 1:
                raise SchemaError(f"ledger database is not a single-link regular file: {self.path}")
            linked = os.stat(self.path.name, dir_fd=directory_fds[-1], follow_symlinks=False)
            identity = _file_identity(pinned)
            if identity != _file_identity(linked) or linked.st_nlink != 1:
                raise SchemaError("ledger database changed while being pinned")
            if self._expected_identity is not None and identity != self._expected_identity:
                raise SchemaError("ledger path no longer names this Ledger instance's bound database")
            self._database_fd = database_fd
            self._directory_fds = directory_fds
            self._open_identity = identity
            database_fd = None
            directory_fds = ()
            self._fault("before_connect")
            self._validate_live_identity()
            descriptor_path = f"/proc/self/fd/{self._database_fd}"
            if not os.path.exists(descriptor_path):
                raise SchemaError("supported Linux /proc descriptor path is unavailable")
            connection = sqlite3.connect(
                f"file:{descriptor_path}?mode=rw",
                timeout=self._timeout,
                isolation_level=None,
                check_same_thread=False,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            self._connection = connection
            connection = None
            if self._migrate_requested:
                schema_meta = (
                    self._db()
                    .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'")
                    .fetchone()
                )
                if schema_meta is not None:
                    source_version = (
                        self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
                    )
                    restore_fenced_v18_on_failure = source_version is not None and str(source_version[0]) == "18"
            self._ensure_schema()
            self._ensure_h4_store()
            self._validate_live_identity()
            if self._expected_identity is None:
                self._expected_identity = identity
        except sqlite3.DatabaseError as exc:
            restore_failed_migration()
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise SchemaError(f"unable to open workflow ledger {self.path}") from exc
        except OSError as exc:
            restore_failed_migration()
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise SchemaError(f"unable to pin workflow ledger {self.path}") from exc
        except BaseException:
            restore_failed_migration()
            self._close_connection()
            if created and self._expected_identity is None:
                self._cleanup_failed_first_open()
            self.close()
            raise
        finally:
            if connection is not None:
                connection.close()
            if database_fd is not None:
                os.close(database_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _ensure_h4_store(self) -> None:
        """Attach the H4 fact store without changing the frozen H3 schema."""

        connection = self._db()
        sidecar = self.path.with_name(self.path.name + ".h4")
        try:
            metadata = os.lstat(sidecar)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SchemaError("H4 fact store is not a single-link regular file")
        except FileNotFoundError:
            pass
        connection.execute("ATTACH DATABASE ? AS h4", (str(sidecar),))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS h4.lifecycle ("
            "run_id TEXT NOT NULL, milestone_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "phase TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL, occurred_at TEXT NOT NULL, "
            "PRIMARY KEY(run_id, milestone_id, sequence))"
        )

    def _restore_fenced_v18_after_failed_open(self) -> None:
        """Compensate a committed v18-to-v19 rename when the opener later fails.

        Service refresh treats the constructor return as the migration commit
        boundary.  A failure after the SQLite migration transaction commits
        must therefore put the exact fenced predecessor identity back before
        the caller restores its legacy unit.
        """

        connection = self._db()
        version = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        identity = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if version is None or identity is None:
            raise CorruptSchemaError("failed migration opener lost schema metadata")
        if str(version[0]) == "18" and str(identity[0]) == _SCHEMA_IDENTITIES[SchemaVersion(18)]:
            self._validate_schema_metadata(SchemaVersion(18))
            self._validate_shape(SchemaVersion(18))
            self._validate_rows()
            return
        if str(version[0]) != "19" or str(identity[0]) != _SCHEMA_IDENTITIES[SchemaVersion(19)]:
            raise CorruptSchemaError("failed migration opener reached an unknown schema identity")
        supervisor_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'supervisor_authority'"
        ).fetchone()
        harness_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'harness_authority'"
        ).fetchone()
        authority = connection.execute(
            "SELECT requested_shutdown FROM harness_authority WHERE singleton = 1"
        ).fetchone()
        if supervisor_exists is not None or harness_exists is None or authority is None or int(authority[0]) != 1:
            raise CorruptSchemaError("failed migration opener cannot prove the fenced v19 authority")
        with self._transaction(validate_authority=False):
            connection.execute("ALTER TABLE harness_authority RENAME TO supervisor_authority")
            connection.execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(18)],),
            )
            connection.execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
        self._validate_schema_metadata(SchemaVersion(18))
        self._validate_shape(SchemaVersion(18))
        self._validate_rows()

    def _cleanup_failed_first_open(self) -> None:
        if self._database_fd is None or not self._directory_fds or self._open_identity is None:
            return
        pinned = os.fstat(self._database_fd)
        if _file_identity(pinned) != self._open_identity or pinned.st_nlink != 1 or pinned.st_size != 0:
            return
        try:
            linked = os.stat(self.path.name, dir_fd=self._directory_fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            return
        if _file_identity(linked) == self._open_identity and linked.st_nlink == 1 and linked.st_size == 0:
            os.unlink(self.path.name, dir_fd=self._directory_fds[-1])

    def _validate_live_identity(self) -> None:
        if self._database_fd is None or not self._directory_fds or self._open_identity is None:
            raise LedgerClosedError("workflow ledger identity is not pinned")
        pinned = os.fstat(self._database_fd)
        if not stat.S_ISREG(pinned.st_mode) or pinned.st_nlink != 1 or _file_identity(pinned) != self._open_identity:
            raise SchemaError("pinned ledger inode or link count changed")
        if self._expected_identity is not None and self._open_identity != self._expected_identity:
            raise SchemaError("open ledger inode differs from its bound identity")
        try:
            linked = os.stat(self.path.name, dir_fd=self._directory_fds[-1], follow_symlinks=False)
        except OSError as exc:
            raise SchemaError("ledger path entry is unavailable") from exc
        if not stat.S_ISREG(linked.st_mode) or linked.st_nlink != 1 or _file_identity(linked) != self._open_identity:
            raise SchemaError("ledger path no longer names the pinned single-link inode")

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerClosedError("workflow ledger is closed")
        return self._connection

    def schema_columns(
        self,
        table: Literal[
            "schema_meta",
            "runs",
            "milestones",
            "dispatches",
            "events",
            "workspace_leases",
            "executions",
            "sdk_lifecycle_events",
            "execution_integrity",
            "app_native_dispatches",
            "harness_authority",
            "dispatch_queue",
            "attempt_capabilities",
            "successor_outbox",
            "notification_outbox",
            "queue_bindings",
            "worker_liveness",
            "authorized_successors",
            "wake_outbox",
            "diagnostic_ring",
            "retry_policies",
            "control_commands",
            "recovery_controls",
            "controller_decisions",
            "controller_decision_generations",
            "controller_action_outbox",
            "milestone_dependencies",
            "integration_outbox",
        ],
    ) -> tuple[str, ...]:
        """Expose a narrow, immutable schema diagnostic without the raw connection."""

        if table not in _V19_TABLE_DDL:
            raise ValueError(f"unknown owned table: {table!r}")
        return tuple(str(row[1]) for row in self._db().execute(f"PRAGMA table_info({table})").fetchall())

    @property
    def schema_version(self) -> SchemaVersion:
        row = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise CorruptSchemaError("schema_version marker is missing")
        try:
            return SchemaVersion(row[0])
        except ValueError as exc:
            raise CorruptSchemaError("schema_version marker is not an integer") from exc

    @property
    def schema_identity(self) -> str:
        """Return the verified immutable identity of the owned schema."""

        row = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if row is None:
            raise CorruptSchemaError("schema_identity marker is missing")
        return str(row[0])

    @property
    def journal_mode(self) -> str:
        """Return SQLite's read-only journal-mode diagnostic."""

        row = self._db().execute("PRAGMA journal_mode").fetchone()
        if row is None:
            raise SchemaError("SQLite did not report a journal mode")
        return str(row[0])

    def _ensure_schema(self) -> None:
        connection = self._db()
        inventory = _schema_inventory(connection)
        if not inventory:
            self._create_schema()
            return
        if not any(item[0] == "table" and item[1] == "schema_meta" for item in inventory):
            raise CorruptSchemaError("workflow ledger is missing schema metadata")
        try:
            version = self.schema_version
        except sqlite3.DatabaseError as exc:
            raise CorruptSchemaError("unable to read schema metadata") from exc
        if version > CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is newer than supported {CURRENT_SCHEMA_VERSION}")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersion(f"ledger schema {version} is unsupported")
        self._validate_schema_metadata(version)
        if version < CURRENT_SCHEMA_VERSION:
            if self._allow_legacy:
                # Legacy access is deliberately read/transition scoped.  It
                # lets the service arm the predecessor refresh fence without
                # changing its schema underneath an older installed owner.
                self._validate_shape(version)
                try:
                    self._validate_rows()
                except ValueError as exc:
                    raise CorruptSchemaError("workflow ledger contains invalid typed values") from exc
                self._legacy_schema_version = version
                return
            if not self._migrate_requested:
                # A non-migrating opener still proves the predecessor is a
                # canonical, readable ledger before refusing the implicit
                # upgrade.  Migration-enabled openers defer validation to the
                # version-specific migration routine, whose ordering includes
                # legacy residue and recovery repairs before shape checks.
                self._validate_shape(version)
                try:
                    self._validate_rows()
                except ValueError as exc:
                    raise CorruptSchemaError("workflow ledger contains invalid typed values") from exc
                raise MigrationRequired(f"ledger schema {version} requires explicit fenced migration authority")
            self._assert_migration_fenced(version)
            self._migrate(version)
        with self._read_transaction():
            self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
            self._validate_shape(CURRENT_SCHEMA_VERSION)
            try:
                self._validate_rows()
            except ValueError as exc:
                raise CorruptSchemaError("workflow ledger contains invalid typed values") from exc

    def _assert_migration_fenced(self, version: SchemaVersion) -> None:
        """Require the predecessor owner to be durably fenced before upgrade."""

        if version < SchemaVersion(18):
            return
        authority_table = "supervisor_authority" if version == SchemaVersion(18) else "harness_authority"
        row = self._db().execute(f"SELECT requested_shutdown FROM {authority_table} WHERE singleton = 1").fetchone()
        if row is not None and int(row["requested_shutdown"]) != 1:
            raise MigrationRequired(
                f"schema {version} migration requires the predecessor {authority_table} refresh fence"
            )

    def _migrate_empty_v9_draft(self) -> None:
        row = (
            self._db()
            .execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'app_native_dispatches'")
            .fetchone()
        )
        if row is None or _canonical_ddl(str(row[0])) != _canonical_ddl(_APP_NATIVE_DISPATCHES_DRAFT_V9_DDL):
            return
        if int(self._db().execute("SELECT COUNT(*) FROM app_native_dispatches").fetchone()[0]) != 0:
            raise UnsupportedSchemaVersion("non-empty draft schema v9 requires explicit recovery before upgrade")
        with self._transaction(validate_authority=False):
            self._db().execute("DROP TABLE app_native_dispatches")
            self._db().execute(_APP_NATIVE_DISPATCHES_DDL)

    def _validate_schema_metadata(self, expected_version: SchemaVersion) -> None:
        version = self.schema_version
        if version != expected_version:
            raise CorruptSchemaError(
                f"schema version changed during validation: expected {expected_version}, found {version}"
            )
        metadata_keys = {
            str(row[0]) for row in self._db().execute("SELECT key FROM schema_meta ORDER BY key").fetchall()
        }
        if metadata_keys != {"schema_version", "migration_marker", "schema_identity"}:
            raise CorruptSchemaError("schema metadata keys are not the owned contract")
        identity = self._db().execute("SELECT value FROM schema_meta WHERE key = 'schema_identity'").fetchone()
        if identity is None or identity[0] != _SCHEMA_IDENTITIES[version]:
            raise CorruptSchemaError("schema identity does not match its version")
        marker = self._db().execute("SELECT value FROM schema_meta WHERE key = 'migration_marker'").fetchone()
        if marker is None and version == CURRENT_SCHEMA_VERSION:
            raise CorruptSchemaError("migration marker is missing")
        expected_markers = {"complete"} if version == CURRENT_SCHEMA_VERSION else {"complete", f"v{int(version)}"}
        if marker is not None and marker[0] not in expected_markers:
            raise CorruptSchemaError("invalid migration marker")

    def _create_schema(self) -> None:
        try:
            with self._transaction(validate_authority=False):
                for statement in _V19_TABLE_DDL.values():
                    self._db().execute(statement)
                for _name, (_table, statement) in _V15_INDEX_DDL.items():
                    self._db().execute(statement)
                self._db().execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(int(CURRENT_SCHEMA_VERSION)),),
                )
                self._db().execute("INSERT INTO schema_meta(key, value) VALUES ('migration_marker', 'complete')")
                self._db().execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_identity', ?)", (_SCHEMA_IDENTITY,)
                )
        except sqlite3.OperationalError as exc:
            # Two first openers may observe an empty file concurrently.  The
            # loser waits for the winner's transaction, then observes the
            # already committed schema and validates it read-only.
            if "already exists" not in str(exc).lower():
                raise
            self._ensure_schema()

    def _has_program_schema_residue(self) -> bool:
        inventory = _schema_inventory(self._db())
        names = {item[1] for item in inventory if item[0] == "table"}
        if names & {"milestone_dependencies", "integration_outbox"}:
            return True
        if "runs" in names:
            run_columns = {str(row[1]) for row in self._db().execute("PRAGMA table_info(runs)").fetchall()}
            if run_columns & {
                "program_digest",
                "program_revision",
                "program_state",
                "program_graph_json",
                "trunk_head",
            }:
                return True
        if "controller_decisions" in names:
            decision_columns = {
                str(row[1]) for row in self._db().execute("PRAGMA table_info(controller_decisions)").fetchall()
            }
            if decision_columns & {"program_id", "program_revision", "event_kind", "event_key"}:
                return True
        return False

    def _has_complete_program_schema(self) -> bool:
        inventory = _schema_inventory(self._db())
        names = {item[1] for item in inventory if item[0] == "table"}
        if not {"milestone_dependencies", "integration_outbox", "runs", "controller_decisions"}.issubset(names):
            return False
        run_columns = {str(row[1]) for row in self._db().execute("PRAGMA table_info(runs)").fetchall()}
        decision_columns = {
            str(row[1]) for row in self._db().execute("PRAGMA table_info(controller_decisions)").fetchall()
        }
        return {
            "program_digest",
            "program_revision",
            "program_state",
            "program_graph_json",
            "trunk_head",
        }.issubset(run_columns) and {"program_id", "program_revision", "event_kind", "event_key"}.issubset(
            decision_columns
        )

    def _validate_shape(self, version: SchemaVersion, *, allow_program_residue: bool = False) -> None:
        if _schema_inventory(self._db(), temporary=True):
            raise CorruptSchemaError("ledger connection has unexpected temporary schema objects")
        definitions = {
            SchemaVersion(1): _V1_TABLE_DDL,
            SchemaVersion(2): _V2_TABLE_DDL,
            SchemaVersion(3): _V3_TABLE_DDL,
            SchemaVersion(4): _V4_TABLE_DDL,
            SchemaVersion(5): _V5_TABLE_DDL,
            SchemaVersion(6): _V6_TABLE_DDL,
            SchemaVersion(7): _V7_TABLE_DDL,
            SchemaVersion(8): _V8_TABLE_DDL,
            SchemaVersion(9): _V9_TABLE_DDL,
            SchemaVersion(10): _V10_TABLE_DDL,
            SchemaVersion(11): _V11_TABLE_DDL,
            SchemaVersion(12): _V12_TABLE_DDL,
            SchemaVersion(13): _V13_TABLE_DDL,
            SchemaVersion(14): _V14_TABLE_DDL,
            SchemaVersion(15): _V15_TABLE_DDL,
            SchemaVersion(16): _V16_TABLE_DDL,
            SchemaVersion(17): _V17_TABLE_DDL,
            SchemaVersion(18): _V18_TABLE_DDL,
            SchemaVersion(19): _V19_TABLE_DDL,
        }[version]
        actual_inventory = _schema_inventory(self._db())
        expected_inventory = _owned_inventory(definitions)
        if version >= SchemaVersion(15):
            expected_inventory = tuple(
                sorted(
                    (
                        *expected_inventory,
                        *tuple(("index", name, table, sql) for name, (table, sql) in _V15_INDEX_DDL.items()),
                    )
                )
            )
        if version == SchemaVersion(1) or version >= SchemaVersion(18):
            actual_inventory = _canonical_inventory(actual_inventory)
            expected_inventory = _canonical_inventory(expected_inventory)
        program_tables = {"milestone_dependencies", "integration_outbox"}
        program_columns = {
            "program_digest",
            "program_revision",
            "program_state",
            "program_graph_json",
            "trunk_head",
            "dispatch_id",
            "program_id",
            "event_kind",
            "event_key",
        }
        if (allow_program_residue or self._has_program_schema_residue()) and version < SchemaVersion(18):
            program_dispatch_table = next(
                (item for item in actual_inventory if item[0] == "table" and item[1] == "dispatches"),
                None,
            )
            actual_inventory = tuple(
                item
                for item in actual_inventory
                if not (item[0] == "table" and item[1] in program_tables | {"runs", "controller_decisions"})
                and not (item[0] == "index" and item[1] == "controller_decisions_program_id_event_key_idx")
            )
            expected_inventory = tuple(
                item
                for item in expected_inventory
                if not (item[0] == "table" and item[1] in program_tables | {"runs", "controller_decisions"})
            )
            if program_dispatch_table is not None and program_dispatch_table[3] is not None:
                if _canonical_ddl(str(program_dispatch_table[3])) == _canonical_ddl(_PROGRAM_DISPATCHES_DDL):
                    expected_inventory = tuple(
                        program_dispatch_table if item[0] == "table" and item[1] == "dispatches" else item
                        for item in expected_inventory
                    )
        # A v19 ledger can be deliberately opened under an older marker by a
        # recovery fixture.  Its live authority already has the replacement
        # harness name; accept that exact table in place of the historical
        # supervisor spelling while the forward migration chain catches up.
        if version < SchemaVersion(19):
            harness_actual = next(
                (item for item in actual_inventory if item[0] == "table" and item[1] == "harness_authority"),
                None,
            )
            supervisor_expected = next(
                (item for item in expected_inventory if item[0] == "table" and item[1] == "supervisor_authority"),
                None,
            )
            if harness_actual is not None and supervisor_expected is not None:
                expected_inventory = tuple(
                    sorted(harness_actual if item == supervisor_expected else item for item in expected_inventory)
                )
        if _inventory_fingerprint(actual_inventory) != _inventory_fingerprint(expected_inventory):
            raise CorruptSchemaError("ledger sqlite_master inventory does not match the canonical owned schema")

        expected: dict[str, dict[str, tuple[str, int, int]]] = {
            "schema_meta": {
                "key": ("TEXT", 1, 1),
                "value": ("TEXT", 1, 0),
            },
            "runs": {
                "run_id": ("TEXT", 1, 1),
                "created_at": ("TEXT", 1, 0),
                "closed_at": ("TEXT", 0, 0),
                "metadata_json": ("TEXT", 1, 0),
            },
            "milestones": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "current_state": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
                "metadata_json": ("TEXT", 1, 0),
            },
            "dispatches": {
                "dispatch_id": ("TEXT", 1, 1),
                "run_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "role": ("TEXT", 1, 0),
                "generation": ("INTEGER", 1, 0),
                "claimed_at": ("TEXT", 1, 0),
            },
            "events": {
                "event_id": ("TEXT", 1, 1),
                "run_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "sequence": ("INTEGER", 1, 0),
                "from_state": ("TEXT", 0, 0),
                "to_state": ("TEXT", 1, 0),
                "event_type": ("TEXT", 1, 0),
                "reason_code": ("TEXT", 0, 0),
                "reason_detail": ("TEXT", 0, 0),
                "dispatch_id": ("TEXT", 0, 0),
                "data_json": ("TEXT", 0, 0),
                "occurred_at": ("TEXT", 1, 0),
            },
            "workspace_leases": {
                "workspace_path": ("TEXT", 1, 1),
                "repository_root": ("TEXT", 1, 0),
                "mode": ("TEXT", 1, 0),
                "branch": ("TEXT", 1, 0),
                "base_sha": ("TEXT", 1, 0),
                "lane": ("TEXT", 1, 0),
                "owner_run_id": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
            },
            "executions": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "capsule_path": ("TEXT", 1, 0),
                "capsule_sha256": ("TEXT", 1, 0),
                "workspace_path": ("TEXT", 1, 0),
                "model": ("TEXT", 1, 0),
                "reasoning_effort": ("TEXT", 1, 0),
                "status": ("TEXT", 1, 0),
                "checkpoint": ("TEXT", 1, 0),
                "thread_id": ("TEXT", 0, 0),
                "turn_id": ("TEXT", 0, 0),
                "turn_output_json": ("TEXT", 0, 0),
                "result_json": ("TEXT", 0, 0),
                "validation_argv_json": ("TEXT", 0, 0),
                "validation_exit_code": ("INTEGER", 0, 0),
                "validation_stdout_sha256": ("TEXT", 0, 0),
                "validation_stderr_sha256": ("TEXT", 0, 0),
                "validation_timed_out": ("INTEGER", 0, 0),
                "validation_duration_seconds": ("REAL", 0, 0),
                "protected_before_sha256": ("TEXT", 1, 0),
                "protected_after_sha256": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "sdk_lifecycle_events": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "turn_id": ("TEXT", 1, 3),
                "sequence": ("INTEGER", 1, 4),
                "method": ("TEXT", 1, 0),
                "event_turn_id": ("TEXT", 0, 0),
            },
            "execution_integrity": {
                "run_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "provenance": ("TEXT", 1, 0),
                "native_profile_sha256": ("TEXT", 0, 0),
                "native_compatibility_sha256": ("TEXT", 0, 0),
                "effective_permission_json": ("TEXT", 0, 0),
                "effective_permission_sha256": ("TEXT", 0, 0),
                "workspace_baseline_head_sha": ("TEXT", 0, 0),
                "workspace_baseline_json": ("TEXT", 0, 0),
                "workspace_baseline_sha256": ("TEXT", 0, 0),
                "workspace_terminal_head_sha": ("TEXT", 0, 0),
                "workspace_terminal_json": ("TEXT", 0, 0),
                "workspace_terminal_sha256": ("TEXT", 0, 0),
                "turn_started_at": ("TEXT", 0, 0),
                "git_authority_before_sha256": ("TEXT", 0, 0),
                "git_authority_after_sha256": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "app_native_dispatches": {
                "dispatch_id": ("TEXT", 1, 1),
                "run_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "state": ("TEXT", 1, 0),
                "action_json": ("TEXT", 1, 0),
                "action_sha256": ("TEXT", 1, 0),
                "claim_token_sha256": ("TEXT", 1, 0),
                "host_id": ("TEXT", 0, 0),
                "thread_id": ("TEXT", 0, 0),
                "result_json": ("TEXT", 0, 0),
                "result_sha256": ("TEXT", 0, 0),
                "workspace_baseline_head_sha": ("TEXT", 1, 0),
                "workspace_baseline_json": ("TEXT", 1, 0),
                "workspace_baseline_sha256": ("TEXT", 1, 0),
                "git_authority_before_sha256": ("TEXT", 1, 0),
                "git_authority_after_sha256": ("TEXT", 0, 0),
                "controller_state_sha256": ("TEXT", 1, 0),
                "workspace_terminal_head_sha": ("TEXT", 0, 0),
                "workspace_terminal_json": ("TEXT", 0, 0),
                "workspace_terminal_sha256": ("TEXT", 0, 0),
                "prepared_at": ("TEXT", 1, 0),
                "bound_at": ("TEXT", 0, 0),
                "completed_at": ("TEXT", 0, 0),
            },
        }
        if version == SchemaVersion(1):
            expected["runs"].pop("closed_at")
        if version < SchemaVersion(3):
            expected.pop("workspace_leases")
            expected.pop("executions")
            expected.pop("sdk_lifecycle_events")
        if version < SchemaVersion(4):
            expected.pop("execution_integrity")
        elif version == SchemaVersion(4):
            expected["execution_integrity"]["sandbox_policy_sha256"] = expected["execution_integrity"].pop(
                "native_profile_sha256"
            )
            expected["execution_integrity"].pop("native_compatibility_sha256")
            expected["execution_integrity"].pop("effective_permission_json")
            expected["execution_integrity"].pop("effective_permission_sha256")
        elif version == SchemaVersion(5):
            expected["execution_integrity"].pop("native_compatibility_sha256")
            expected["execution_integrity"].pop("effective_permission_json")
            expected["execution_integrity"].pop("effective_permission_sha256")
        if version < SchemaVersion(7) and "execution_integrity" in expected:
            expected["execution_integrity"].pop("workspace_baseline_head_sha")
            expected["execution_integrity"].pop("workspace_baseline_json")
            expected["execution_integrity"].pop("workspace_baseline_sha256")
            expected["execution_integrity"].pop("turn_started_at")
        if version < SchemaVersion(8) and "execution_integrity" in expected:
            expected["execution_integrity"].pop("workspace_terminal_head_sha")
            expected["execution_integrity"].pop("workspace_terminal_json")
            expected["execution_integrity"].pop("workspace_terminal_sha256")
        if version < SchemaVersion(9):
            expected.pop("app_native_dispatches")
        authority_name = "harness_authority" if version >= SchemaVersion(19) else "supervisor_authority"
        if version < SchemaVersion(10):
            expected.pop("supervisor_authority", None)
            expected.pop("harness_authority", None)
            expected.pop("dispatch_queue", None)
            expected.pop("attempt_capabilities", None)
            expected.pop("successor_outbox", None)
            expected.pop("notification_outbox", None)
        if version >= SchemaVersion(10):
            expected.update(
                {
                    authority_name: {
                        "singleton": ("INTEGER", 1, 1),
                        "repository_root": ("TEXT", 1, 0),
                        "state_root": ("TEXT", 1, 0),
                        "epoch": ("INTEGER", 1, 0),
                        "owner_nonce_sha256": ("TEXT", 1, 0),
                        "pid": ("INTEGER", 1, 0),
                        "process_birth_identity": ("TEXT", 1, 0),
                        "acquired_at": ("TEXT", 1, 0),
                        "renewed_at": ("TEXT", 1, 0),
                        "expires_at": ("TEXT", 1, 0),
                        "executable_digest": ("TEXT", 1, 0),
                        "version": ("TEXT", 1, 0),
                        "requested_shutdown": ("INTEGER", 1, 0),
                    },
                    "dispatch_queue": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "run_id": ("TEXT", 1, 0),
                        "milestone_id": ("TEXT", 1, 0),
                        "role": ("TEXT", 1, 0),
                        "generation": ("INTEGER", 1, 0),
                        "backend": ("TEXT", 1, 0),
                        "capsule_json": ("TEXT", 1, 0),
                        "action_json": ("TEXT", 0, 0),
                        "route_json": ("TEXT", 1, 0),
                        "workspace_path": ("TEXT", 1, 0),
                        "result_contract_sha256": ("TEXT", 1, 0),
                        "state": ("TEXT", 1, 0),
                        "sequence": ("INTEGER", 1, 0),
                        "available_at": ("TEXT", 1, 0),
                        "deadline": ("TEXT", 0, 0),
                        "claim_epoch": ("INTEGER", 0, 0),
                        "claim_nonce_sha256": ("TEXT", 0, 0),
                        "attempt": ("INTEGER", 1, 0),
                        "thread_id": ("TEXT", 0, 0),
                        "host_id": ("TEXT", 0, 0),
                        "raw_result_json": ("TEXT", 0, 0),
                        "raw_result_sha256": ("TEXT", 0, 0),
                        "terminal_status": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                    "attempt_capabilities": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "generation": ("INTEGER", 1, 2),
                        "attempt": ("INTEGER", 1, 3),
                        "operation": ("TEXT", 1, 0),
                        "schema_sha256": ("TEXT", 1, 0),
                        "workspace_path": ("TEXT", 1, 0),
                        "backend": ("TEXT", 1, 0),
                        "issued_at": ("TEXT", 1, 0),
                        "expires_at": ("TEXT", 1, 0),
                        "consumed_at": ("TEXT", 0, 0),
                        "token_sha256": ("TEXT", 1, 0),
                        "accepted_result_sha256": ("TEXT", 0, 0),
                    },
                    "successor_outbox": {
                        "source_dispatch_id": ("TEXT", 1, 1),
                        "successor_dispatch_id": ("TEXT", 0, 0),
                        "payload_json": ("TEXT", 1, 0),
                        "outcome": ("TEXT", 1, 0),
                        "created_at": ("TEXT", 1, 0),
                        "completed_at": ("TEXT", 0, 0),
                    },
                    "notification_outbox": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "source_task": ("TEXT", 0, 0),
                        "payload_digest": ("TEXT", 1, 0),
                        "outcome": ("TEXT", 1, 0),
                        "attempted_at": ("TEXT", 0, 0),
                    },
                }
            )
        if version >= SchemaVersion(11):
            expected.update(
                {
                    "queue_bindings": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "plan_path": ("TEXT", 1, 0),
                        "plan_revision_sha256": ("TEXT", 1, 0),
                        "projection_sha256": ("TEXT", 1, 0),
                        "source_thread_id": ("TEXT", 0, 0),
                        "permission_mode": ("TEXT", 1, 0),
                        "native_profile_sha256": ("TEXT", 0, 0),
                        "native_compatibility_sha256": ("TEXT", 0, 0),
                        "effective_permission_json": ("TEXT", 0, 0),
                        "effective_permission_sha256": ("TEXT", 0, 0),
                        "checkpoint_deadline": ("TEXT", 0, 0),
                        "checkpoint_armed": ("INTEGER", 1, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                    "worker_liveness": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "generation": ("INTEGER", 1, 0),
                        "attempt": ("INTEGER", 1, 0),
                        "pid": ("INTEGER", 1, 0),
                        "process_birth_identity": ("TEXT", 1, 0),
                        "lease_token_sha256": ("TEXT", 1, 0),
                        "lease_expires_at": ("TEXT", 1, 0),
                        "last_seen_at": ("TEXT", 1, 0),
                        "exited_at": ("TEXT", 0, 0),
                    },
                    "authorized_successors": {
                        "source_dispatch_id": ("TEXT", 1, 1),
                        "successor_dispatch_id": ("TEXT", 1, 2),
                        "authorized_at": ("TEXT", 1, 0),
                        "released_at": ("TEXT", 0, 0),
                    },
                    "wake_outbox": {
                        "delivery_id": ("TEXT", 1, 1),
                        "dispatch_id": ("TEXT", 1, 0),
                        "kind": ("TEXT", 1, 0),
                        "source_thread_id": ("TEXT", 0, 0),
                        "payload_json": ("TEXT", 1, 0),
                        "payload_digest": ("TEXT", 1, 0),
                        "state": ("TEXT", 1, 0),
                        "attempt_count": ("INTEGER", 1, 0),
                        "source_turn_id": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                }
            )
        if version >= SchemaVersion(12):
            expected["recovery_state"] = {
                "dispatch_id": ("TEXT", 1, 1),
                "provider_env_key": ("TEXT", 0, 0),
                "profile_sha256": ("TEXT", 0, 0),
                "worker_exit_classification": ("TEXT", 0, 0),
                "exit_code": ("INTEGER", 0, 0),
                "recovery_state": ("TEXT", 1, 0),
                "inspection_budget": ("INTEGER", 1, 0),
                "inspection_used": ("INTEGER", 1, 0),
                "continuation_budget": ("INTEGER", 1, 0),
                "continuation_used": ("INTEGER", 1, 0),
                "fresh_thread_after_idle": ("INTEGER", 1, 0),
                "fresh_thread_budget": ("INTEGER", 1, 0),
                "fresh_thread_used": ("INTEGER", 1, 0),
                "next_eligible_at": ("TEXT", 0, 0),
                "inspected_thread_id": ("TEXT", 0, 0),
                "inspected_turn_id": ("TEXT", 0, 0),
                "inspected_kind": ("TEXT", 0, 0),
                "human_attention_reason": ("TEXT", 0, 0),
                "exited_at": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            }
        if version >= SchemaVersion(13):
            expected.update(
                {
                    "diagnostic_ring": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "sequence": ("INTEGER", 1, 2),
                        "kind": ("TEXT", 1, 0),
                        "occurred_at": ("TEXT", 1, 0),
                        "text": ("TEXT", 0, 0),
                        "payload_sha256": ("TEXT", 0, 0),
                        "payload_bytes": ("INTEGER", 1, 0),
                    },
                    "retry_policies": {
                        "dispatch_id": ("TEXT", 1, 1),
                        "revision": ("INTEGER", 1, 0),
                        "policy_version": ("INTEGER", 1, 0),
                        "pre_identity_budget": ("INTEGER", 1, 0),
                        "invalid_chain_budget": ("INTEGER", 1, 0),
                        "schema_envelope_budget": ("INTEGER", 1, 0),
                        "post_identity_loss_budget": ("INTEGER", 1, 0),
                        "pre_identity_used": ("INTEGER", 1, 0),
                        "invalid_chain_used": ("INTEGER", 1, 0),
                        "schema_envelope_used": ("INTEGER", 1, 0),
                        "post_identity_loss_used": ("INTEGER", 1, 0),
                        "last_failure": ("TEXT", 0, 0),
                        "strategy": ("TEXT", 1, 0),
                        "next_eligible_at": ("TEXT", 0, 0),
                        "prior_thread_id": ("TEXT", 0, 0),
                        "prior_turn_id": ("TEXT", 0, 0),
                        "human_attention_reason": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                    "control_commands": {
                        "command_id": ("TEXT", 1, 1),
                        "dispatch_id": ("TEXT", 1, 0),
                        "submission_sequence": ("INTEGER", 1, 0),
                        "generation": ("INTEGER", 1, 0),
                        "attempt": ("INTEGER", 1, 0),
                        "thread_id": ("TEXT", 1, 0),
                        "turn_id": ("TEXT", 1, 0),
                        "kind": ("TEXT", 1, 0),
                        "payload": ("TEXT", 0, 0),
                        "payload_sha256": ("TEXT", 1, 0),
                        "state": ("TEXT", 1, 0),
                        "acknowledgement_json": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "sent_at": ("TEXT", 0, 0),
                        "acknowledged_at": ("TEXT", 0, 0),
                    },
                    "recovery_controls": {
                        "action_id": ("TEXT", 1, 1),
                        "dispatch_id": ("TEXT", 1, 0),
                        "expected_revision": ("INTEGER", 1, 0),
                        "action_kind": ("TEXT", 1, 0),
                        "reason": ("TEXT", 1, 0),
                        "requested_budget_json": ("TEXT", 0, 0),
                        "applied_revision": ("INTEGER", 1, 0),
                        "created_at": ("TEXT", 1, 0),
                    },
                }
            )
            if version == SchemaVersion(13):
                expected["control_commands"].pop("submission_sequence")
                expected["retry_policies"].pop("revision")
                expected.pop("recovery_controls")
            if version >= SchemaVersion(16):
                expected["recovery_controls"]["compatibility_rebind_json"] = ("TEXT", 0, 0)
            if version >= SchemaVersion(17):
                expected["retry_policies"].update(
                    {
                        "provider_transient_budget": ("INTEGER", 1, 0),
                        "provider_transient_used": ("INTEGER", 1, 0),
                        "provider_transient_grant_used": ("INTEGER", 1, 0),
                    }
                )
        if version >= SchemaVersion(15):
            expected.update(
                {
                    "controller_decisions": {
                        "decision_id": ("TEXT", 1, 1),
                        "dispatch_id": ("TEXT", 1, 0),
                        "kind": ("TEXT", 1, 0),
                        "cycle_sequence": ("INTEGER", 1, 0),
                        "summary_json": ("TEXT", 1, 0),
                        "summary_sha256": ("TEXT", 1, 0),
                        "source_thread_id": ("TEXT", 0, 0),
                        "state": ("TEXT", 1, 0),
                        "revision": ("INTEGER", 1, 0),
                        "current_generation": ("INTEGER", 1, 0),
                        "generation_budget": ("INTEGER", 1, 0),
                        "generation_used": ("INTEGER", 1, 0),
                        "claimant_kind": ("TEXT", 0, 0),
                        "claimant_id": ("TEXT", 0, 0),
                        "claim_token_sha256": ("TEXT", 0, 0),
                        "claim_started_at": ("TEXT", 0, 0),
                        "claim_lease_expires_at": ("TEXT", 0, 0),
                        "action_id": ("TEXT", 0, 0),
                        "action_bundle_json": ("TEXT", 0, 0),
                        "action_bundle_sha256": ("TEXT", 0, 0),
                        "committed_at": ("TEXT", 0, 0),
                        "acknowledged_at": ("TEXT", 0, 0),
                        "superseded_at": ("TEXT", 0, 0),
                        "deadline": ("TEXT", 1, 0),
                        "human_attention_reason": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                    "controller_decision_generations": {
                        "decision_id": ("TEXT", 1, 1),
                        "generation": ("INTEGER", 1, 2),
                        "lineage_id": ("TEXT", 1, 0),
                        "predecessor_generation": ("INTEGER", 0, 0),
                        "source_kind": ("TEXT", 1, 0),
                        "state": ("TEXT", 1, 0),
                        "prompt_sha256": ("TEXT", 0, 0),
                        "controller_thread_id": ("TEXT", 0, 0),
                        "controller_turn_id": ("TEXT", 0, 0),
                        "inspection_started_at": ("TEXT", 0, 0),
                        "inspection_token_sha256": ("TEXT", 0, 0),
                        "inspection_lease_expires_at": ("TEXT", 0, 0),
                        "inspection_completed_at": ("TEXT", 0, 0),
                        "inspection_outcome": ("TEXT", 0, 0),
                        "inspection_bundle_json": ("TEXT", 0, 0),
                        "inspection_bundle_sha256": ("TEXT", 0, 0),
                        "started_at": ("TEXT", 0, 0),
                        "terminal_at": ("TEXT", 0, 0),
                        "created_at": ("TEXT", 1, 0),
                        "updated_at": ("TEXT", 1, 0),
                    },
                    "controller_action_outbox": {
                        "action_id": ("TEXT", 1, 1),
                        "decision_id": ("TEXT", 1, 0),
                        "generation": ("INTEGER", 1, 0),
                        "expected_revision": ("INTEGER", 1, 0),
                        "claimant_kind": ("TEXT", 1, 0),
                        "claimant_id": ("TEXT", 1, 0),
                        "bundle_json": ("TEXT", 1, 0),
                        "bundle_sha256": ("TEXT", 1, 0),
                        "effect_receipt_json": ("TEXT", 1, 0),
                        "effect_receipt_sha256": ("TEXT", 1, 0),
                        "state": ("TEXT", 1, 0),
                        "committed_at": ("TEXT", 1, 0),
                        "acknowledged_at": ("TEXT", 0, 0),
                    },
                }
            )
            expected["wake_outbox"]["decision_id"] = ("TEXT", 1, 0)
            expected["wake_outbox"]["cycle_sequence"] = ("INTEGER", 1, 0)
        if version >= SchemaVersion(18):
            expected["runs"].update(
                {
                    "program_digest": ("TEXT", 0, 0),
                    "program_revision": ("INTEGER", 1, 0),
                    "program_state": ("TEXT", 0, 0),
                    "program_graph_json": ("TEXT", 0, 0),
                    "trunk_head": ("TEXT", 0, 0),
                }
            )
            expected["controller_decisions"].update(
                {
                    "dispatch_id": ("TEXT", 0, 0),
                    "program_id": ("TEXT", 0, 0),
                    "program_revision": ("INTEGER", 0, 0),
                    "event_kind": ("TEXT", 0, 0),
                    "event_key": ("TEXT", 0, 0),
                }
            )
            expected["milestone_dependencies"] = {
                "program_id": ("TEXT", 1, 1),
                "milestone_id": ("TEXT", 1, 2),
                "dependency_milestone_id": ("TEXT", 1, 3),
                "created_at": ("TEXT", 1, 0),
            }
            expected["integration_outbox"] = {
                "integration_id": ("TEXT", 1, 1),
                "program_id": ("TEXT", 1, 0),
                "milestone_id": ("TEXT", 1, 0),
                "candidate_sha": ("TEXT", 1, 0),
                "expected_trunk_head": ("TEXT", 1, 0),
                "strategy": ("TEXT", 1, 0),
                "state": ("TEXT", 1, 0),
                "receipt_json": ("TEXT", 0, 0),
                "receipt_sha256": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "completed_at": ("TEXT", 0, 0),
            }
        for table, expected_columns in expected.items():
            actual_table = table
            if table == "supervisor_authority":
                harness_present = (
                    self._db()
                    .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'harness_authority'")
                    .fetchone()
                )
                supervisor_present = (
                    self._db()
                    .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'supervisor_authority'")
                    .fetchone()
                )
                if harness_present is not None and supervisor_present is None:
                    actual_table = "harness_authority"
            rows = self._db().execute(f"PRAGMA table_info({actual_table})").fetchall()
            actual = {str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows}
            if (allow_program_residue or self._has_program_schema_residue()) and version < SchemaVersion(18):
                actual = {name: value for name, value in actual.items() if name not in program_columns}
                expected_columns = {
                    name: value for name, value in expected_columns.items() if name not in program_columns
                }
            if actual != expected_columns:
                raise CorruptSchemaError(f"ledger table {table!r} has an unexpected column contract")

        dispatch_table = (
            self._db().execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dispatches'").fetchone()
        )
        dispatches_use_generation_identity = version >= SchemaVersion(18) or (
            dispatch_table is not None
            and dispatch_table[0] is not None
            and _canonical_ddl(str(dispatch_table[0])) == _canonical_ddl(_PROGRAM_DISPATCHES_DDL)
        )
        self._require_unique_index(
            "dispatches",
            ("run_id", "milestone_id", "role", "generation")
            if dispatches_use_generation_identity
            else ("run_id", "milestone_id", "role"),
        )
        self._require_unique_index("events", ("run_id", "milestone_id", "sequence"))
        self._require_foreign_keys(
            "milestones",
            (("runs", "run_id", "run_id", "CASCADE"),),
        )
        self._require_foreign_keys(
            "dispatches",
            (("milestones", "run_id", "run_id", "CASCADE"), ("milestones", "milestone_id", "milestone_id", "CASCADE")),
        )
        self._require_foreign_keys(
            "events",
            (
                ("milestones", "run_id", "run_id", "CASCADE"),
                ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                ("dispatches", "dispatch_id", "dispatch_id", "RESTRICT"),
            ),
        )
        if version >= SchemaVersion(3):
            self._require_unique_index("workspace_leases", ("owner_run_id", "lane"))
            self._require_foreign_keys(
                "workspace_leases",
                (("runs", "owner_run_id", "run_id", "CASCADE"),),
            )
            self._require_foreign_keys(
                "executions",
                (
                    ("milestones", "run_id", "run_id", "CASCADE"),
                    ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
            self._require_foreign_keys(
                "sdk_lifecycle_events",
                (
                    ("executions", "run_id", "run_id", "CASCADE"),
                    ("executions", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        if version >= SchemaVersion(4):
            self._require_foreign_keys(
                "execution_integrity",
                (
                    ("executions", "run_id", "run_id", "CASCADE"),
                    ("executions", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        if version >= SchemaVersion(9):
            self._require_unique_index("app_native_dispatches", ("run_id", "milestone_id"))
            self._require_foreign_keys(
                "app_native_dispatches",
                (
                    ("dispatches", "dispatch_id", "dispatch_id", "RESTRICT"),
                    ("executions", "run_id", "run_id", "CASCADE"),
                    ("executions", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        if version >= SchemaVersion(10):
            self._require_unique_index("dispatch_queue", ("sequence",))
            self._require_unique_index("attempt_capabilities", ("dispatch_id", "generation", "attempt"))
            self._require_foreign_keys("dispatch_queue", (("dispatches", "dispatch_id", "dispatch_id", "RESTRICT"),))
            self._require_foreign_keys(
                "attempt_capabilities", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
            self._require_foreign_keys(
                "successor_outbox",
                (
                    ("dispatch_queue", "source_dispatch_id", "dispatch_id", "CASCADE"),
                    ("dispatch_queue", "successor_dispatch_id", "dispatch_id", "RESTRICT"),
                ),
            )
            self._require_foreign_keys(
                "notification_outbox", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
        if version >= SchemaVersion(11):
            self._require_foreign_keys("queue_bindings", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),))
            self._require_foreign_keys(
                "worker_liveness", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
            self._require_foreign_keys(
                "authorized_successors",
                (
                    ("dispatch_queue", "source_dispatch_id", "dispatch_id", "CASCADE"),
                    ("dispatch_queue", "successor_dispatch_id", "dispatch_id", "RESTRICT"),
                ),
            )
            self._require_unique_index(
                "wake_outbox",
                ("dispatch_id", "kind", "cycle_sequence") if version >= SchemaVersion(15) else ("dispatch_id", "kind"),
            )
            if version < SchemaVersion(15):
                self._require_foreign_keys(
                    "wake_outbox", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
                )
        if version >= SchemaVersion(12):
            self._require_foreign_keys("recovery_state", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),))
        if version >= SchemaVersion(13):
            self._require_foreign_keys(
                "diagnostic_ring", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
            self._require_foreign_keys("retry_policies", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),))
            self._require_foreign_keys(
                "control_commands", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
        if version >= SchemaVersion(14):
            self._require_unique_index("control_commands", ("dispatch_id", "submission_sequence"))
            self._require_foreign_keys(
                "recovery_controls", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
        if version >= SchemaVersion(15):
            expected["controller_decisions"] = {
                "decision_id": ("TEXT", 1, 1),
                "dispatch_id": ("TEXT", 1, 0),
                "kind": ("TEXT", 1, 0),
                "cycle_sequence": ("INTEGER", 1, 0),
                "summary_json": ("TEXT", 1, 0),
                "summary_sha256": ("TEXT", 1, 0),
                "source_thread_id": ("TEXT", 0, 0),
                "state": ("TEXT", 1, 0),
                "revision": ("INTEGER", 1, 0),
                "current_generation": ("INTEGER", 1, 0),
                "generation_budget": ("INTEGER", 1, 0),
                "generation_used": ("INTEGER", 1, 0),
                "claimant_kind": ("TEXT", 0, 0),
                "claimant_id": ("TEXT", 0, 0),
                "claim_token_sha256": ("TEXT", 0, 0),
                "claim_started_at": ("TEXT", 0, 0),
                "claim_lease_expires_at": ("TEXT", 0, 0),
                "action_id": ("TEXT", 0, 0),
                "action_bundle_json": ("TEXT", 0, 0),
                "action_bundle_sha256": ("TEXT", 0, 0),
                "committed_at": ("TEXT", 0, 0),
                "acknowledged_at": ("TEXT", 0, 0),
                "superseded_at": ("TEXT", 0, 0),
                "deadline": ("TEXT", 1, 0),
                "human_attention_reason": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            }
            expected["controller_decision_generations"] = {
                "decision_id": ("TEXT", 1, 1),
                "generation": ("INTEGER", 1, 2),
                "lineage_id": ("TEXT", 1, 0),
                "predecessor_generation": ("INTEGER", 0, 0),
                "source_kind": ("TEXT", 1, 0),
                "state": ("TEXT", 1, 0),
                "prompt_sha256": ("TEXT", 0, 0),
                "controller_thread_id": ("TEXT", 0, 0),
                "controller_turn_id": ("TEXT", 0, 0),
                "inspection_started_at": ("TEXT", 0, 0),
                "inspection_token_sha256": ("TEXT", 0, 0),
                "inspection_lease_expires_at": ("TEXT", 0, 0),
                "inspection_completed_at": ("TEXT", 0, 0),
                "inspection_outcome": ("TEXT", 0, 0),
                "inspection_bundle_json": ("TEXT", 0, 0),
                "inspection_bundle_sha256": ("TEXT", 0, 0),
                "started_at": ("TEXT", 0, 0),
                "terminal_at": ("TEXT", 0, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            }
            expected["controller_action_outbox"] = {
                "action_id": ("TEXT", 1, 1),
                "decision_id": ("TEXT", 1, 0),
                "generation": ("INTEGER", 1, 0),
                "expected_revision": ("INTEGER", 1, 0),
                "claimant_kind": ("TEXT", 1, 0),
                "claimant_id": ("TEXT", 1, 0),
                "bundle_json": ("TEXT", 1, 0),
                "bundle_sha256": ("TEXT", 1, 0),
                "effect_receipt_json": ("TEXT", 1, 0),
                "effect_receipt_sha256": ("TEXT", 1, 0),
                "state": ("TEXT", 1, 0),
                "committed_at": ("TEXT", 1, 0),
                "acknowledged_at": ("TEXT", 0, 0),
            }
            expected["wake_outbox"]["decision_id"] = ("TEXT", 1, 0)
            expected["wake_outbox"]["cycle_sequence"] = ("INTEGER", 1, 0)
            self._require_unique_index("wake_outbox", ("decision_id",))
            self._require_unique_index("controller_decisions", ("dispatch_id", "kind", "cycle_sequence"))
            self._require_unique_index("controller_decisions", ("decision_id",))
            self._require_unique_index("controller_decision_generations", ("decision_id", "generation"))
            self._require_unique_index("controller_action_outbox", ("decision_id",))
            self._require_foreign_keys(
                "controller_decisions", (("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),)
            )
            self._require_foreign_keys(
                "controller_decision_generations", (("controller_decisions", "decision_id", "decision_id", "CASCADE"),)
            )
            self._require_foreign_keys(
                "controller_action_outbox", (("controller_decisions", "decision_id", "decision_id", "CASCADE"),)
            )
            self._require_foreign_keys(
                "wake_outbox",
                (
                    ("dispatch_queue", "dispatch_id", "dispatch_id", "CASCADE"),
                    ("controller_decisions", "decision_id", "decision_id", "CASCADE"),
                ),
            )
        if version >= SchemaVersion(18):
            self._require_unique_index(
                "milestone_dependencies", ("program_id", "milestone_id", "dependency_milestone_id")
            )
            self._require_unique_index("integration_outbox", ("program_id", "milestone_id", "candidate_sha"))
            self._require_unique_index("controller_decisions", ("program_id", "event_key"))
            self._require_foreign_keys(
                "milestone_dependencies",
                (
                    ("milestones", "program_id", "run_id", "CASCADE"),
                    ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                    ("milestones", "program_id", "run_id", "CASCADE"),
                    ("milestones", "dependency_milestone_id", "milestone_id", "CASCADE"),
                ),
            )
            self._require_foreign_keys(
                "integration_outbox",
                (
                    ("milestones", "program_id", "run_id", "CASCADE"),
                    ("milestones", "milestone_id", "milestone_id", "CASCADE"),
                ),
            )
        foreign_keys = self._db().execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CorruptSchemaError("foreign-key enforcement is disabled")

    def _require_unique_index(self, table: str, columns: tuple[str, ...]) -> None:
        for row in self._db().execute(f"PRAGMA index_list({table})").fetchall():
            if int(row[2]) != 1:
                continue
            index_columns = tuple(
                str(info[2]) for info in self._db().execute(f"PRAGMA index_info({row[1]})").fetchall()
            )
            if index_columns == columns:
                return
        raise CorruptSchemaError(f"ledger table {table!r} is missing unique index {columns!r}")

    def _require_foreign_keys(self, table: str, expected: tuple[tuple[str, str, str, str], ...]) -> None:
        actual = tuple(
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
            for row in sorted(
                self._db().execute(f"PRAGMA foreign_key_list({table})").fetchall(),
                key=lambda row: (int(row[0]), int(row[1])),
            )
        )
        if sorted(actual) != sorted(expected):
            raise CorruptSchemaError(f"ledger table {table!r} has unexpected foreign keys")

    def _validate_rows(self, *, allow_exited_active: bool = False) -> None:
        has_execution_tables = any(item[1] == "executions" for item in _schema_inventory(self._db()))
        has_integrity = any(item[1] == "execution_integrity" for item in _schema_inventory(self._db()))
        has_app_native = any(item[1] == "app_native_dispatches" for item in _schema_inventory(self._db()))
        has_dispatch_queue = any(item[1] == "dispatch_queue" for item in _schema_inventory(self._db()))
        has_queue_bindings = any(item[1] == "queue_bindings" for item in _schema_inventory(self._db()))
        has_recovery_state = any(item[1] == "recovery_state" for item in _schema_inventory(self._db()))
        has_live_control = any(item[1] == "diagnostic_ring" for item in _schema_inventory(self._db()))
        has_command_sequence = (
            any(
                str(row[1]) == "submission_sequence"
                for row in self._db().execute("PRAGMA table_info(control_commands)").fetchall()
            )
            if has_live_control
            else False
        )
        has_recovery_controls = any(item[1] == "recovery_controls" for item in _schema_inventory(self._db()))
        has_causal_workspace = has_integrity and any(
            str(row[1]) == "turn_started_at"
            for row in self._db().execute("PRAGMA table_info(execution_integrity)").fetchall()
        )
        orphan_queries = [
            "SELECT COUNT(*) FROM milestones m LEFT JOIN runs r ON r.run_id = m.run_id WHERE r.run_id IS NULL",
            "SELECT COUNT(*) FROM dispatches d LEFT JOIN milestones m ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN milestones m ON m.run_id = e.run_id AND m.milestone_id = e.milestone_id WHERE m.run_id IS NULL",
            "SELECT COUNT(*) FROM events e LEFT JOIN dispatches d ON d.dispatch_id = e.dispatch_id WHERE e.dispatch_id IS NOT NULL AND d.dispatch_id IS NULL",
        ]
        if has_execution_tables:
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM workspace_leases w LEFT JOIN runs r ON r.run_id = w.owner_run_id WHERE r.run_id IS NULL",
                    "SELECT COUNT(*) FROM executions x LEFT JOIN milestones m ON m.run_id = x.run_id AND m.milestone_id = x.milestone_id WHERE m.run_id IS NULL",
                    "SELECT COUNT(*) FROM sdk_lifecycle_events s LEFT JOIN executions x ON x.run_id = s.run_id AND x.milestone_id = s.milestone_id WHERE x.run_id IS NULL",
                )
            )
        if has_integrity:
            orphan_queries.append(
                "SELECT COUNT(*) FROM execution_integrity i LEFT JOIN executions x "
                "ON x.run_id = i.run_id AND x.milestone_id = i.milestone_id WHERE x.run_id IS NULL"
            )
        if has_app_native:
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM app_native_dispatches a LEFT JOIN dispatches d "
                    "ON d.dispatch_id = a.dispatch_id WHERE d.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM app_native_dispatches a LEFT JOIN executions x "
                    "ON x.run_id = a.run_id AND x.milestone_id = a.milestone_id WHERE x.run_id IS NULL",
                )
            )
        if has_dispatch_queue:
            # Queue facts are intentionally closed and immutable.  Foreign-key
            # checks above cover ownership; these lightweight invariants keep
            # malformed rows from becoming a recovery authority.
            authority_table = (
                "harness_authority"
                if self._db()
                .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'harness_authority'")
                .fetchone()
                else "supervisor_authority"
            )
            if int(self._db().execute(f"SELECT COUNT(*) FROM {authority_table}").fetchone()[0]) > 1:
                raise CorruptSchemaError("multiple harness authority rows")
            duplicate = (
                self._db()
                .execute(
                    "SELECT dispatch_id, generation, attempt, COUNT(*) FROM attempt_capabilities "
                    "GROUP BY dispatch_id, generation, attempt HAVING COUNT(*) > 1"
                )
                .fetchone()
            )
            if duplicate is not None:
                raise CorruptSchemaError("duplicate attempt capability fact")
            duplicate = (
                self._db()
                .execute(
                    "SELECT dispatch_id, COUNT(*) FROM notification_outbox GROUP BY dispatch_id HAVING COUNT(*) > 1"
                )
                .fetchone()
            )
            if duplicate is not None:
                raise CorruptSchemaError("duplicate terminal notification fact")
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM dispatch_queue q LEFT JOIN dispatches d "
                    "ON d.dispatch_id = q.dispatch_id WHERE d.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM attempt_capabilities c LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = c.dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM successor_outbox s LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = s.source_dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM notification_outbox n LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = n.dispatch_id WHERE q.dispatch_id IS NULL",
                )
            )
            for queue_row in self._db().execute("SELECT * FROM dispatch_queue ORDER BY sequence").fetchall():
                dispatch_row = (
                    self._db()
                    .execute(
                        "SELECT run_id, milestone_id, role, generation FROM dispatches WHERE dispatch_id = ?",
                        (queue_row["dispatch_id"],),
                    )
                    .fetchone()
                )
                if dispatch_row is None or (
                    str(queue_row["run_id"]),
                    str(queue_row["milestone_id"]),
                    str(queue_row["role"]),
                    int(queue_row["generation"]),
                ) != (
                    str(dispatch_row["run_id"]),
                    str(dispatch_row["milestone_id"]),
                    str(dispatch_row["role"]),
                    int(dispatch_row["generation"]),
                ):
                    raise CorruptSchemaError("queue dispatch identity does not match legacy dispatch authority")
                if queue_row["state"] in {"completed", "failed"}:
                    raw = queue_row["raw_result_json"]
                    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65_536:
                        raise CorruptSchemaError("queue terminal raw result exceeds its bound")
                    try:
                        parsed_result = self._parse_queue_result(str(queue_row["dispatch_id"]), raw)
                    except (TypeError, ValueError) as exc:
                        raise CorruptSchemaError("queue terminal raw result is malformed") from exc
                    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != queue_row["raw_result_sha256"]:
                        raise CorruptSchemaError("queue terminal raw result digest is invalid")
                    if self._queue_result_terminal_status(parsed_result) != queue_row["terminal_status"]:
                        raise CorruptSchemaError("queue terminal status does not match raw result")
            for capability in self._db().execute("SELECT * FROM attempt_capabilities").fetchall():
                queue_row = (
                    self._db()
                    .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (capability["dispatch_id"],))
                    .fetchone()
                )
                if queue_row is None or (
                    int(capability["generation"]),
                    int(capability["attempt"]),
                    str(capability["backend"]),
                    str(capability["workspace_path"]),
                    str(capability["schema_sha256"]),
                ) != (
                    int(queue_row["generation"]),
                    int(queue_row["attempt"]),
                    str(queue_row["backend"]),
                    str(queue_row["workspace_path"]),
                    str(queue_row["result_contract_sha256"]),
                ):
                    raise CorruptSchemaError("attempt capability binding conflicts with queue")
                consumed = capability["consumed_at"] is not None
                accepted = capability["accepted_result_sha256"]
                if consumed != (accepted is not None):
                    raise CorruptSchemaError("attempt capability consumed/accepted facts are misaligned")
                if queue_row["state"] == "cancelled":
                    raise CorruptSchemaError("cancelled queue row retains an attempt capability")
                if consumed and queue_row["state"] not in {"result_submitted", "finalizing", "completed", "failed"}:
                    raise CorruptSchemaError("consumed capability is attached to a non-result queue state")
                if consumed and queue_row["raw_result_sha256"] != accepted:
                    raise CorruptSchemaError("consumed capability digest does not match queue result")
            for queue_row in self._db().execute("SELECT * FROM dispatch_queue ORDER BY sequence").fetchall():
                state = str(queue_row["state"])
                has_raw_json = queue_row["raw_result_json"] is not None
                has_raw_digest = queue_row["raw_result_sha256"] is not None
                has_raw = has_raw_json and has_raw_digest
                if has_raw_json != has_raw_digest:
                    raise CorruptSchemaError("queue raw result fact is partial")
                if state == "result_submitted" and (not has_raw or queue_row["terminal_status"] is None):
                    raise CorruptSchemaError("submitted queue row lacks its raw result fact")
                if state in {"completed", "failed"} and (not has_raw or queue_row["terminal_status"] is None):
                    raise CorruptSchemaError("result terminal queue row lacks immutable result fact")
                if state in {
                    "queued",
                    "claimed",
                    "starting",
                    "running",
                    "recovery_inspection_pending",
                    "recovery_retry_wait",
                    "recovery_continuation_pending",
                    "human_attention_required",
                } and (has_raw or queue_row["terminal_status"]):
                    raise CorruptSchemaError("nonterminal queue row carries terminal result facts")
                if state == "cancelled" and (has_raw or queue_row["terminal_status"] is not None):
                    raise CorruptSchemaError("cancelled queue row carries a fabricated result fact")
                if state == "cancelled" and (
                    queue_row["claim_epoch"] is not None or queue_row["claim_nonce_sha256"] is not None
                ):
                    raise CorruptSchemaError("cancelled queue row retains a claim fact")
                successor = (
                    self._db()
                    .execute(
                        "SELECT successor_dispatch_id, outcome, completed_at FROM successor_outbox WHERE source_dispatch_id = ?",
                        (queue_row["dispatch_id"],),
                    )
                    .fetchone()
                )
                notification = (
                    self._db()
                    .execute(
                        "SELECT outcome, attempted_at FROM notification_outbox WHERE dispatch_id = ?",
                        (queue_row["dispatch_id"],),
                    )
                    .fetchone()
                )
                if state in {"completed", "failed"}:
                    if successor is None or notification is None:
                        raise CorruptSchemaError("terminal queue row lacks successor/notification outbox facts")
                elif state == "cancelled" and (successor is not None or notification is not None):
                    raise CorruptSchemaError("cancelled queue row carries successor/notification facts")
                if successor is not None:
                    outcome = str(successor["outcome"])
                    successor_id = successor["successor_dispatch_id"]
                    if outcome == "enqueued":
                        if not isinstance(successor_id, str) or successor_id == queue_row["dispatch_id"]:
                            raise CorruptSchemaError("enqueued successor fact is missing a distinct dispatch")
                        if (
                            self._db()
                            .execute("SELECT 1 FROM dispatch_queue WHERE dispatch_id = ?", (successor_id,))
                            .fetchone()
                            is None
                        ):
                            raise CorruptSchemaError("enqueued successor fact points outside the queue")
                        if successor["completed_at"] is None:
                            raise CorruptSchemaError("enqueued successor fact is incomplete")
                    elif outcome == "not_required" and successor_id is not None:
                        raise CorruptSchemaError("not-required successor fact carries a dispatch")
                    elif outcome == "pending" and successor["completed_at"] is not None:
                        raise CorruptSchemaError("pending successor fact is marked complete")
                if (
                    notification is not None
                    and str(notification["outcome"]).startswith("attempted_")
                    and notification["attempted_at"] is None
                ):
                    raise CorruptSchemaError("notification attempt lacks its timestamp")
        if has_queue_bindings:
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM queue_bindings b LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = b.dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM worker_liveness l LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = l.dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM authorized_successors a LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = a.source_dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM wake_outbox w LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = w.dispatch_id WHERE q.dispatch_id IS NULL",
                )
            )
            queue_ids = {
                str(row["dispatch_id"])
                for row in self._db().execute("SELECT dispatch_id FROM dispatch_queue").fetchall()
            }
            binding_rows = self._db().execute("SELECT * FROM queue_bindings ORDER BY dispatch_id").fetchall()
            if {str(row["dispatch_id"]) for row in binding_rows} != queue_ids:
                raise CorruptSchemaError("every queue dispatch must have exactly one immutable v11 binding")
            bindings_by_dispatch = {str(row["dispatch_id"]): row for row in binding_rows}
            for binding in binding_rows:
                if not str(binding["plan_path"]):
                    raise CorruptSchemaError("queue plan identity is missing")
                try:
                    _sha256(str(binding["plan_revision_sha256"]), field_name="queue plan revision digest")
                    _sha256(str(binding["projection_sha256"]), field_name="queue projection digest")
                    if binding["source_thread_id"] is not None:
                        ThreadIdentity(str(binding["source_thread_id"]))
                    permission_mode = NativePermissionMode(str(binding["permission_mode"]))
                except (TypeError, ValueError) as exc:
                    raise CorruptSchemaError("queue binding identity is malformed") from exc
                permission_raw = binding["effective_permission_json"]
                permission_digest = binding["effective_permission_sha256"]
                if permission_raw is not None:
                    permission = _decode_json_object(str(permission_raw), field_name="queue permission facts")
                    if permission is None:
                        raise CorruptSchemaError("queue permission facts are missing")
                    try:
                        authority = NativePermissionAuthority.from_facts(permission)
                    except (TypeError, ValueError) as exc:
                        raise CorruptSchemaError("queue permission facts are malformed") from exc
                    if hashlib.sha256(_encode_json(authority.facts).encode("utf-8")).hexdigest() != permission_digest:
                        raise CorruptSchemaError("queue permission digest does not match its facts")
                    if authority.mode is not permission_mode:
                        raise CorruptSchemaError("queue permission mode conflicts with its effective authority")
                if (binding["checkpoint_deadline"] is None) != (int(binding["checkpoint_armed"]) == 0):
                    raise CorruptSchemaError("queue checkpoint arm facts are inconsistent")
            for live in self._db().execute("SELECT * FROM worker_liveness ORDER BY dispatch_id").fetchall():
                queue = (
                    self._db()
                    .execute(
                        "SELECT generation, attempt, state FROM dispatch_queue WHERE dispatch_id = ?",
                        (live["dispatch_id"],),
                    )
                    .fetchone()
                )
                if queue is None or (int(live["generation"]), int(live["attempt"])) != (
                    int(queue["generation"]),
                    int(queue["attempt"]),
                ):
                    raise CorruptSchemaError("worker liveness identity conflicts with queue attempt")
                if (
                    not allow_exited_active
                    and live["exited_at"] is not None
                    and queue["state"] in {"queued", "claimed", "starting", "running"}
                ):
                    raise CorruptSchemaError("queue retains an active state after its bound worker exited")
                if queue["state"] == "cancelled" and live["exited_at"] is None:
                    raise CorruptSchemaError("cancelled queue retains an unacknowledged live worker")
            for authorized in self._db().execute("SELECT * FROM authorized_successors").fetchall():
                if (
                    str(authorized["source_dispatch_id"]) not in queue_ids
                    or str(authorized["successor_dispatch_id"]) not in queue_ids
                ):
                    raise CorruptSchemaError("authorized successor points outside the queue")
                if authorized["released_at"] is not None:
                    source = (
                        self._db()
                        .execute(
                            "SELECT state FROM dispatch_queue WHERE dispatch_id = ?",
                            (authorized["source_dispatch_id"],),
                        )
                        .fetchone()
                    )
                    if source is None or source["state"] not in {"completed", "failed"}:
                        raise CorruptSchemaError("successor released before source terminal commit")
            wake_rows = self._db().execute("SELECT * FROM wake_outbox ORDER BY delivery_id").fetchall()
            wake_by_dispatch: dict[str, list[sqlite3.Row]] = {}
            for wake in wake_rows:
                dispatch_id = str(wake["dispatch_id"])
                wake_by_dispatch.setdefault(dispatch_id, []).append(wake)
                if "decision_id" in wake.keys():
                    decision_ref = (
                        self._db()
                        .execute(
                            "SELECT dispatch_id, kind FROM controller_decisions WHERE decision_id = ?",
                            (wake["decision_id"],),
                        )
                        .fetchone()
                    )
                    if (
                        decision_ref is None
                        or str(decision_ref["dispatch_id"]) != dispatch_id
                        or str(decision_ref["kind"]) != str(wake["kind"])
                    ):
                        raise CorruptSchemaError("wake delivery decision identity is conflicting")
                payload = _decode_json_object(str(wake["payload_json"]), field_name="wake payload")
                if (
                    payload is None
                    or hashlib.sha256(str(wake["payload_json"]).encode("utf-8")).hexdigest() != wake["payload_digest"]
                ):
                    raise CorruptSchemaError("wake payload digest does not match its facts")
                expected_delivery_id = (
                    f"wake/{dispatch_id}/{wake['kind']}"
                    if int(wake["cycle_sequence"]) == 1
                    else f"wake/{dispatch_id}/{wake['kind']}/{int(wake['cycle_sequence'])}"
                )
                if str(wake["delivery_id"]) != expected_delivery_id:
                    raise CorruptSchemaError("wake delivery identity is not deterministic")
                if "cycle_sequence" in payload and payload["cycle_sequence"] != int(wake["cycle_sequence"]):
                    raise CorruptSchemaError("wake payload cycle sequence is conflicting")
                binding = bindings_by_dispatch.get(dispatch_id)
                queue = (
                    self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
                )
                if binding is None or queue is None:
                    raise CorruptSchemaError("wake delivery lacks queue binding authority")
                if wake["source_thread_id"] != binding["source_thread_id"] and wake["state"] != "suppressed":
                    raise CorruptSchemaError("wake source identity conflicts with the queue binding")
                if _encode_json(payload) != str(wake["payload_json"]):
                    raise CorruptSchemaError("wake payload is not canonically serialized")
                if payload.get("schema_version") != 1 or payload.get("delivery_id") != wake["delivery_id"]:
                    raise CorruptSchemaError("wake payload identity is conflicting")
                if wake["kind"] == "terminal":
                    expected_payload = {
                        "schema_version": 1,
                        "kind": "TERMINAL",
                        "delivery_id": str(wake["delivery_id"]),
                        "dispatch_id": dispatch_id,
                        "state": str(queue["state"]),
                        "result_sha256": queue["raw_result_sha256"],
                        "ledger_path": os.fspath(self.path),
                    }
                    if payload != expected_payload:
                        raise CorruptSchemaError("terminal wake payload conflicts with its result authority")
                elif wake["kind"] == "checkpoint":
                    live = (
                        self._db()
                        .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (dispatch_id,))
                        .fetchone()
                    )
                    if "decision_id" in wake.keys() and set(payload) in (
                        {"schema_version", "kind", "delivery_id", "dispatch_id", "state", "ledger_path"},
                        {
                            "schema_version",
                            "kind",
                            "delivery_id",
                            "dispatch_id",
                            "cycle_sequence",
                            "state",
                            "ledger_path",
                        },
                    ):
                        # A direct typed decision client may create a
                        # checkpoint wake before a worker snapshot exists.
                        pass
                    elif set(payload) in (
                        {
                            "schema_version",
                            "kind",
                            "delivery_id",
                            "dispatch_id",
                            "state",
                            "rearmed_at",
                            "ledger_path",
                        },
                        {
                            "schema_version",
                            "kind",
                            "delivery_id",
                            "dispatch_id",
                            "cycle_sequence",
                            "state",
                            "rearmed_at",
                            "ledger_path",
                        },
                    ):
                        if (
                            not isinstance(payload["rearmed_at"], str)
                            or not payload["rearmed_at"]
                            or payload["state"]
                            not in {
                                "queued",
                                "claimed",
                                "starting",
                                "running",
                                "result_submitted",
                                "human_attention_required",
                            }
                        ):
                            raise CorruptSchemaError("rearmed checkpoint payload has invalid facts")
                        expected_rearmed_payload = {
                            "schema_version": 1,
                            "kind": "CHECKPOINT",
                            "delivery_id": str(wake["delivery_id"]),
                            "dispatch_id": dispatch_id,
                            "state": str(queue["state"]),
                            "rearmed_at": payload["rearmed_at"],
                            "ledger_path": os.fspath(self.path),
                        }
                        if "cycle_sequence" in payload:
                            expected_rearmed_payload["cycle_sequence"] = int(wake["cycle_sequence"])
                        if payload != expected_rearmed_payload:
                            raise CorruptSchemaError("rearmed checkpoint payload is conflicting")
                    elif set(payload) in (
                        {
                            "schema_version",
                            "kind",
                            "delivery_id",
                            "dispatch_id",
                            "state",
                            "thread_id",
                            "pid",
                            "process_birth_identity",
                            "started_at",
                            "last_seen_at",
                            "lease_expires_at",
                            "ledger_path",
                        },
                        {
                            "schema_version",
                            "kind",
                            "delivery_id",
                            "dispatch_id",
                            "cycle_sequence",
                            "state",
                            "thread_id",
                            "pid",
                            "process_birth_identity",
                            "started_at",
                            "last_seen_at",
                            "lease_expires_at",
                            "ledger_path",
                        },
                    ):
                        if payload["state"] not in {
                            "claimed",
                            "starting",
                            "running",
                            "result_submitted",
                            "human_attention_required",
                        }:
                            raise CorruptSchemaError("checkpoint payload has an invalid queue state")
                        if "cycle_sequence" in payload and payload["cycle_sequence"] != int(wake["cycle_sequence"]):
                            raise CorruptSchemaError("checkpoint payload cycle sequence is invalid")
                        if (
                            payload["started_at"] != queue["created_at"]
                            or payload["ledger_path"] != os.fspath(self.path)
                            or (payload["thread_id"] is not None and not isinstance(payload["thread_id"], str))
                            or (payload["pid"] is None) != (payload["process_birth_identity"] is None)
                            or (payload["pid"] is not None and not isinstance(payload["pid"], int))
                            or (
                                payload["process_birth_identity"] is not None
                                and not isinstance(payload["process_birth_identity"], str)
                            )
                            or (payload["last_seen_at"] is None) != (payload["lease_expires_at"] is None)
                            or (payload["last_seen_at"] is not None and not isinstance(payload["last_seen_at"], str))
                            or (
                                payload["lease_expires_at"] is not None
                                and not isinstance(payload["lease_expires_at"], str)
                            )
                        ):
                            raise CorruptSchemaError("checkpoint payload has conflicting snapshot facts")
                    else:
                        raise CorruptSchemaError("checkpoint wake payload shape is unsupported")
                else:  # pragma: no cover - SQLite CHECK is the first guard
                    raise CorruptSchemaError("wake kind is unsupported")
                if wake["state"] == "pending" and int(wake["attempt_count"]) >= 2:
                    raise CorruptSchemaError("pending wake has exhausted its retry budget")
                if wake["state"] == "failed" and int(wake["attempt_count"]) != 2:
                    raise CorruptSchemaError("failed wake does not carry its exhausted retry budget")
            for queue in self._db().execute("SELECT dispatch_id, state FROM dispatch_queue").fetchall():
                wakes = wake_by_dispatch.get(str(queue["dispatch_id"]), [])
                checkpoint_wakes = [wake for wake in wakes if wake["kind"] == "checkpoint"]
                if len(checkpoint_wakes) > 64 or len({int(wake["cycle_sequence"]) for wake in checkpoint_wakes}) != len(
                    checkpoint_wakes
                ):
                    raise CorruptSchemaError("queue dispatch has duplicate checkpoint cycles")
                if sorted(int(wake["cycle_sequence"]) for wake in checkpoint_wakes) != list(
                    range(1, len(checkpoint_wakes) + 1)
                ):
                    raise CorruptSchemaError("checkpoint cycles are not contiguous")
                if len([wake for wake in wakes if wake["kind"] == "terminal"]) > 1:
                    raise CorruptSchemaError("queue dispatch has duplicate wake deliveries")
                terminal_wakes = [wake for wake in wakes if wake["kind"] == "terminal"]
                if queue["state"] in {"completed", "failed"} and len(terminal_wakes) != 1:
                    raise CorruptSchemaError("terminal queue dispatch lacks exactly one terminal wake")
                if queue["state"] == "cancelled" and wakes:
                    raise CorruptSchemaError("cancelled queue dispatch carries a wake")
        if has_recovery_state:
            orphan_queries.append(
                "SELECT COUNT(*) FROM recovery_state r LEFT JOIN dispatch_queue q "
                "ON q.dispatch_id = r.dispatch_id WHERE q.dispatch_id IS NULL"
            )
            queue_ids = {
                str(row["dispatch_id"])
                for row in self._db().execute("SELECT dispatch_id FROM dispatch_queue").fetchall()
            }
            recovery_rows = self._db().execute("SELECT * FROM recovery_state ORDER BY dispatch_id").fetchall()
            if {str(row["dispatch_id"]) for row in recovery_rows} != queue_ids:
                raise CorruptSchemaError("every queue dispatch must have exactly one v12 recovery state")
            for recovery in recovery_rows:
                profile = recovery["profile_sha256"]
                if profile is not None and not re.fullmatch(r"[0-9a-f]{64}", str(profile)):
                    raise CorruptSchemaError("recovery profile identity is malformed")
                key = recovery["provider_env_key"]
                if key is not None and re.fullmatch(r"[A-Z][A-Z0-9_]*", str(key)) is None:
                    raise CorruptSchemaError("recovery provider key name is malformed")
                if int(recovery["inspection_used"]) > int(recovery["inspection_budget"]):
                    raise CorruptSchemaError("recovery inspection budget regressed")
                if int(recovery["continuation_used"]) > int(recovery["continuation_budget"]):
                    raise CorruptSchemaError("recovery continuation budget regressed")
                if int(recovery["fresh_thread_used"]) > int(recovery["fresh_thread_budget"]):
                    raise CorruptSchemaError("recovery fresh-thread budget regressed")
                recovery_kind = str(recovery["recovery_state"])
                reason = recovery["human_attention_reason"]
                if recovery_kind == "human_attention_required" and not isinstance(reason, str):
                    raise CorruptSchemaError("human-attention recovery lacks a bounded reason")
                if recovery_kind != "human_attention_required" and reason is not None:
                    raise CorruptSchemaError("nonterminal recovery carries human-attention reason")
                queue = (
                    self._db()
                    .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (recovery["dispatch_id"],))
                    .fetchone()
                )
                queue_state = str(queue["state"]) if queue is not None else ""
                eligible = recovery["next_eligible_at"]
                if recovery_kind in {"recovery_retry_wait", "recovery_continuation_pending"}:
                    if not isinstance(eligible, str) or not eligible or len(eligible) > 64:
                        raise CorruptSchemaError("bounded recovery state lacks next eligibility")
                    try:
                        parsed_eligibility = datetime.fromisoformat(
                            eligible[:-1] + "+00:00" if eligible.endswith("Z") else eligible
                        )
                    except ValueError as exc:
                        raise CorruptSchemaError("recovery eligibility is not a valid timestamp") from exc
                    if parsed_eligibility.tzinfo is None:
                        raise CorruptSchemaError("recovery eligibility must be timezone-aware")
                elif eligible is not None:
                    raise CorruptSchemaError("terminal or idle recovery carries unexpected eligibility")
                if (
                    recovery_kind
                    in {
                        "recovery_inspection_pending",
                        "recovery_retry_wait",
                        "recovery_continuation_pending",
                        "human_attention_required",
                    }
                    and queue_state != recovery_kind
                ):
                    raise CorruptSchemaError("queue state conflicts with recovery state")
                if recovery_kind in {"completed", "failed"} and queue_state != recovery_kind:
                    raise CorruptSchemaError("terminal recovery state conflicts with queue state")

        if has_live_control:
            orphan_queries.extend(
                (
                    "SELECT COUNT(*) FROM diagnostic_ring d LEFT JOIN dispatch_queue q ON q.dispatch_id = d.dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM retry_policies r LEFT JOIN dispatch_queue q ON q.dispatch_id = r.dispatch_id WHERE q.dispatch_id IS NULL",
                    "SELECT COUNT(*) FROM control_commands c LEFT JOIN dispatch_queue q ON q.dispatch_id = c.dispatch_id WHERE q.dispatch_id IS NULL",
                )
            )
            if has_recovery_controls:
                orphan_queries.append(
                    "SELECT COUNT(*) FROM recovery_controls a LEFT JOIN dispatch_queue q "
                    "ON q.dispatch_id = a.dispatch_id WHERE q.dispatch_id IS NULL"
                )
            queue_ids = {
                str(row["dispatch_id"])
                for row in self._db().execute("SELECT dispatch_id FROM dispatch_queue").fetchall()
            }
            retry_rows = self._db().execute("SELECT * FROM retry_policies ORDER BY dispatch_id").fetchall()
            if {str(row["dispatch_id"]) for row in retry_rows} != queue_ids:
                raise CorruptSchemaError("every queue dispatch must have one live-worker retry policy")
            for retry in retry_rows:
                retry_budgets = [
                    (retry["pre_identity_used"], retry["pre_identity_budget"]),
                    (retry["invalid_chain_used"], retry["invalid_chain_budget"]),
                    (retry["schema_envelope_used"], retry["schema_envelope_budget"]),
                    (retry["post_identity_loss_used"], retry["post_identity_loss_budget"]),
                ]
                provider_retry_schema = "provider_transient_budget" in retry.keys()
                if provider_retry_schema:
                    retry_budgets.append((retry["provider_transient_used"], retry["provider_transient_budget"]))
                for used, budget in retry_budgets:
                    if int(used) < 0 or int(used) > int(budget):
                        raise CorruptSchemaError("retry policy consumed count exceeds its immutable budget")
                if provider_retry_schema:
                    if int(retry["provider_transient_grant_used"]) not in {0, 1}:
                        raise CorruptSchemaError("provider transient grant fact is invalid")
                    if int(retry["provider_transient_budget"]) > 3 + int(retry["provider_transient_grant_used"]):
                        raise CorruptSchemaError("provider transient budget lacks its grant fact")
                if retry["last_failure"] is not None:
                    try:
                        RetryFailureClass(str(retry["last_failure"]))
                    except ValueError as exc:
                        raise CorruptSchemaError("retry policy failure class is unknown") from exc
                try:
                    RecoveryStrategy(str(retry["strategy"]))
                except ValueError as exc:
                    raise CorruptSchemaError("retry policy strategy is unknown") from exc
                if (
                    retry["strategy"] == RecoveryStrategy.HUMAN_ATTENTION.value
                    and retry["human_attention_reason"] is None
                ):
                    raise CorruptSchemaError("human-attention retry state lacks a reason")
                if (
                    retry["strategy"] != RecoveryStrategy.HUMAN_ATTENTION.value
                    and retry["human_attention_reason"] is not None
                ):
                    raise CorruptSchemaError("nonterminal retry state carries a human-attention reason")
            diagnostic_rows = (
                self._db().execute("SELECT * FROM diagnostic_ring ORDER BY dispatch_id, sequence").fetchall()
            )
            by_dispatch: dict[str, list[sqlite3.Row]] = {}
            for item in diagnostic_rows:
                by_dispatch.setdefault(str(item["dispatch_id"]), []).append(item)
                if int(item["payload_bytes"]) > DIAGNOSTIC_TEXT_MAX_BYTES:
                    raise CorruptSchemaError("diagnostic payload exceeds its byte bound")
                if item["text"] is not None:
                    try:
                        redacted = str(item["text"])
                        if (
                            len(redacted.encode("utf-8")) > DIAGNOSTIC_TEXT_MAX_BYTES
                            or redact_diagnostic_text(redacted) != redacted
                        ):
                            raise CorruptSchemaError("diagnostic text is unredacted or oversized")
                    except UnicodeEncodeError as exc:
                        raise CorruptSchemaError("diagnostic text is not valid UTF-8") from exc
                if (
                    item["payload_sha256"] is not None
                    and re.fullmatch(r"[0-9a-f]{64}", str(item["payload_sha256"])) is None
                ):
                    raise CorruptSchemaError("diagnostic payload digest is malformed")
            for _dispatch_id, rows in by_dispatch.items():
                if len(rows) > DIAGNOSTIC_RING_MAX_ENTRIES:
                    raise CorruptSchemaError("diagnostic ring exceeds its count bound")
                if sum(int(row["payload_bytes"]) for row in rows) > DIAGNOSTIC_RING_MAX_BYTES:
                    raise CorruptSchemaError("diagnostic ring exceeds its byte bound")
                sequences = [int(row["sequence"]) for row in rows]
                if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
                    raise CorruptSchemaError("diagnostic ring sequence is not monotonic")
            for command in self._db().execute("SELECT * FROM control_commands ORDER BY command_id").fetchall():
                try:
                    ControlCommandKind(str(command["kind"]))
                    ControlCommandState(str(command["state"]))
                    ThreadIdentity(str(command["thread_id"]))
                except ValueError as exc:
                    raise CorruptSchemaError("control command identity or state is malformed") from exc
                if command["payload"] is not None:
                    try:
                        normalized_payload = redact_control_text(str(command["payload"]))
                    except ValueError as exc:
                        raise CorruptSchemaError("control command payload is invalid") from exc
                    if normalized_payload != str(command["payload"]):
                        raise CorruptSchemaError("control command payload is unredacted")
                payload_digest = command["payload_sha256"]
                if (
                    not isinstance(payload_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", payload_digest) is None
                    or hashlib.sha256(str(command["payload"] or "").encode("utf-8")).hexdigest() != payload_digest
                ):
                    raise CorruptSchemaError("control command payload digest is invalid")
                if command["acknowledgement_json"] is not None:
                    if command["state"] not in {"acknowledged", "rejected", "unresolved"}:
                        raise CorruptSchemaError("control command acknowledgement precedes terminal command state")
                    try:
                        acknowledgement = strict_json_loads(str(command["acknowledgement_json"]), max_bytes=4096)
                    except ValueError as exc:
                        raise CorruptSchemaError("control command acknowledgement is invalid") from exc
                    if not isinstance(acknowledgement, dict):
                        raise CorruptSchemaError("control command acknowledgement is not an object")
                    if set(acknowledgement) not in ({"detail"}, {"detail", "terminal_status"}):
                        raise CorruptSchemaError("control command acknowledgement shape is invalid")
                    detail = acknowledgement["detail"]
                    if detail is not None and (
                        not isinstance(detail, str) or redact_control_text(detail, limit=512) != detail
                    ):
                        raise CorruptSchemaError("control command acknowledgement detail is invalid")
                    terminal_status = acknowledgement.get("terminal_status")
                    if terminal_status is not None and terminal_status not in CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES:
                        raise CorruptSchemaError("control command acknowledgement terminal status is invalid")
            if has_command_sequence:
                command_sequences: dict[str, list[int]] = {}
                for command in (
                    self._db()
                    .execute(
                        "SELECT dispatch_id, submission_sequence FROM control_commands ORDER BY dispatch_id, submission_sequence"
                    )
                    .fetchall()
                ):
                    command_sequences.setdefault(str(command["dispatch_id"]), []).append(
                        int(command["submission_sequence"])
                    )
                for _dispatch_id, sequences in command_sequences.items():
                    if sequences != list(range(1, len(sequences) + 1)):
                        raise CorruptSchemaError("control command submission sequence is not contiguous")
            if has_command_sequence and any(int(row["revision"]) < 0 for row in retry_rows):
                raise CorruptSchemaError("retry policy revision is invalid")
            if has_recovery_controls:
                for action in self._db().execute("SELECT * FROM recovery_controls ORDER BY action_id").fetchall():
                    if (
                        int(action["expected_revision"]) < 0
                        or int(action["applied_revision"]) != int(action["expected_revision"]) + 1
                    ):
                        raise CorruptSchemaError("recovery action revision facts are inconsistent")
                    try:
                        action_kind = RecoveryActionKind(str(action["action_kind"]))
                    except ValueError as exc:
                        raise CorruptSchemaError("recovery action kind is unknown") from exc
                    if (
                        not isinstance(action["reason"], str)
                        or not str(action["reason"]).strip()
                        or len(str(action["reason"]).encode("utf-8")) > 512
                    ):
                        raise CorruptSchemaError("recovery action reason is invalid")
                    budget_json = action["requested_budget_json"]
                    rebind_json = (
                        action["compatibility_rebind_json"] if "compatibility_rebind_json" in action.keys() else None
                    )
                    if (action_kind is RecoveryActionKind.BUDGET_CHANGE) != (budget_json is not None):
                        raise CorruptSchemaError("recovery action budget facts are inconsistent")
                    if (action_kind is RecoveryActionKind.COMPATIBILITY_REBIND) != (rebind_json is not None):
                        raise CorruptSchemaError("recovery action compatibility facts are inconsistent")
                    if rebind_json is not None:
                        try:
                            raw_rebind = strict_json_loads(str(rebind_json), max_bytes=1024)
                            legacy_fields = {
                                "expected_compatibility_sha256",
                                "proposed_compatibility_sha256",
                                "expected_generation",
                                "expected_attempt",
                            }
                            current_fields = legacy_fields | {
                                "expected_profile_sha256",
                                "proposed_profile_sha256",
                            }
                            if not isinstance(raw_rebind, dict) or set(raw_rebind) not in {
                                frozenset(legacy_fields),
                                frozenset(current_fields),
                            }:
                                raise ValueError("compatibility rebind shape is invalid")
                            CompatibilityRebind(**raw_rebind)
                        except (TypeError, ValueError) as exc:
                            raise CorruptSchemaError("recovery action compatibility facts are invalid") from exc

        has_controller_decisions = any(item[1] == "controller_decisions" for item in _schema_inventory(self._db()))
        has_program_decisions = has_controller_decisions and any(
            str(row[1]) == "program_id"
            for row in self._db().execute("PRAGMA table_info(controller_decisions)").fetchall()
        )
        if has_controller_decisions:
            orphan_queries.extend(
                (
                    (
                        "SELECT COUNT(*) FROM controller_decisions d LEFT JOIN dispatch_queue q ON q.dispatch_id = d.dispatch_id "
                        "WHERE d.program_id IS NULL AND q.dispatch_id IS NULL"
                        if has_program_decisions
                        else "SELECT COUNT(*) FROM controller_decisions d LEFT JOIN dispatch_queue q ON q.dispatch_id = d.dispatch_id WHERE q.dispatch_id IS NULL"
                    ),
                    "SELECT COUNT(*) FROM controller_decision_generations g LEFT JOIN controller_decisions d ON d.decision_id = g.decision_id WHERE d.decision_id IS NULL",
                    "SELECT COUNT(*) FROM controller_action_outbox a LEFT JOIN controller_decisions d ON d.decision_id = a.decision_id WHERE d.decision_id IS NULL",
                    "SELECT COUNT(*) FROM wake_outbox w LEFT JOIN controller_decisions d ON d.decision_id = w.decision_id WHERE d.decision_id IS NULL",
                )
            )
            decision_rows = self._db().execute("SELECT * FROM controller_decisions ORDER BY decision_id").fetchall()
            for decision in decision_rows:
                if has_program_decisions and decision["program_id"] is not None:
                    self._validate_program_decision_row(decision)
                    continue
                try:
                    ControllerDecisionId(str(decision["decision_id"]))
                    DispatchId(str(decision["dispatch_id"]))
                    ControllerDecisionState(str(decision["state"]))
                except ValueError as exc:
                    raise CorruptSchemaError("controller decision identity or state is malformed") from exc
                if decision["decision_id"] != f"decision/{decision['decision_id'].split('decision/', 1)[-1]}":
                    raise CorruptSchemaError("controller decision identity is not canonical")
                summary = str(decision["summary_json"])
                try:
                    decoded_summary = strict_json_loads(summary, max_bytes=16_384)
                except ValueError as exc:
                    raise CorruptSchemaError("controller decision summary is invalid") from exc
                if (
                    not isinstance(decoded_summary, dict)
                    or hashlib.sha256(summary.encode("utf-8")).hexdigest() != decision["summary_sha256"]
                ):
                    raise CorruptSchemaError("controller decision summary digest is invalid")
                if int(decision["generation_used"]) > int(decision["generation_budget"]):
                    raise CorruptSchemaError("controller generation budget is exceeded")
                claimant_fields = (
                    decision["claimant_kind"],
                    decision["claimant_id"],
                    decision["claim_token_sha256"],
                    decision["claim_started_at"],
                )
                if any(item is None for item in claimant_fields) and not all(item is None for item in claimant_fields):
                    raise CorruptSchemaError("controller claim facts are incomplete")
                if decision["claimant_kind"] is not None:
                    try:
                        ControllerClaimantKind(str(decision["claimant_kind"]))
                    except ValueError as exc:
                        raise CorruptSchemaError("controller claimant kind is invalid") from exc
                action_fields = (
                    decision["action_id"],
                    decision["action_bundle_json"],
                    decision["action_bundle_sha256"],
                    decision["committed_at"],
                )
                if any(item is None for item in action_fields) and not all(item is None for item in action_fields):
                    raise CorruptSchemaError("controller action facts are incomplete")
                if str(decision["state"]) == ControllerDecisionState.CLAIMED.value and (
                    decision["claim_token_sha256"] is None or decision["claim_lease_expires_at"] is None
                ):
                    raise CorruptSchemaError("claimed controller decision lacks lease facts")
                completed_inspection_audit = (
                    self._db()
                    .execute(
                        "SELECT 1 FROM controller_decision_generations "
                        "WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NOT NULL "
                        "AND inspection_outcome = 'completed' AND inspection_bundle_json IS NOT NULL "
                        "AND inspection_bundle_sha256 IS NOT NULL",
                        (str(decision["decision_id"]), int(decision["current_generation"])),
                    )
                    .fetchone()
                    is not None
                )
                if str(decision["state"]) in {
                    ControllerDecisionState.AWAITING_CLAIM.value,
                    ControllerDecisionState.PENDING_DELIVERY.value,
                } and any(item is not None for item in claimant_fields):
                    # After a completed one-read inspection, reap deactivates
                    # the secret capability but deliberately retains the
                    # original model claimant tuple as immutable audit.  It is
                    # valid only in awaiting_claim, with no live lease, and
                    # only while the exact inspected bundle is durable.
                    if not (
                        str(decision["state"]) == ControllerDecisionState.AWAITING_CLAIM.value
                        and str(decision["claimant_kind"]) == ControllerClaimantKind.MODEL.value
                        and decision["claim_lease_expires_at"] is None
                        and completed_inspection_audit
                        and all(item is not None for item in claimant_fields)
                    ):
                        raise CorruptSchemaError("unclaimed controller decision carries claimant facts")
                if str(decision["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value:
                    if decision["action_id"] is None and any(item is not None for item in claimant_fields):
                        raise CorruptSchemaError("unclaimed human-attention decision carries claimant facts")
                    if decision["action_id"] is not None and any(item is None for item in claimant_fields[:4]):
                        raise CorruptSchemaError("human-attention action lacks its claimant audit facts")
                if str(decision["state"]) in {
                    ControllerDecisionState.ACTION_COMMITTED.value,
                    ControllerDecisionState.ACKNOWLEDGED.value,
                } and any(item is None for item in action_fields):
                    raise CorruptSchemaError("committed controller decision lacks action facts")
                if str(decision["state"]) not in {
                    ControllerDecisionState.ACTION_COMMITTED.value,
                    ControllerDecisionState.ACKNOWLEDGED.value,
                    ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value,
                } and any(item is not None for item in action_fields):
                    raise CorruptSchemaError("non-committed controller decision carries action facts")
                if decision["action_id"] is not None:
                    # The decision row and effect outbox are separate durable
                    # facts, but they must describe one exact action.  A
                    # syntactically valid row with a substituted bundle or
                    # digest is corruption, not a replayable receipt.
                    outbox = (
                        self._db()
                        .execute(
                            "SELECT decision_id, generation, expected_revision, bundle_json, bundle_sha256, claimant_kind, claimant_id, state "
                            "FROM controller_action_outbox WHERE action_id = ?",
                            (str(decision["action_id"]),),
                        )
                        .fetchone()
                    )
                    if outbox is None:
                        raise CorruptSchemaError("controller decision action lacks its outbox receipt")
                    if (
                        str(outbox["decision_id"]) != str(decision["decision_id"])
                        or int(outbox["generation"]) != int(decision["current_generation"])
                        or int(outbox["expected_revision"]) + 1 != int(decision["revision"])
                        or str(outbox["bundle_sha256"]) != str(decision["action_bundle_sha256"])
                        or str(outbox["bundle_json"]) != str(decision["action_bundle_json"])
                    ):
                        raise CorruptSchemaError("controller decision action and outbox facts diverge")
                    try:
                        decision_bundle = ModelFacingControllerActionBundle.from_json_bytes(
                            str(decision["action_bundle_json"])
                        )
                    except (TypeError, ValueError) as exc:
                        raise CorruptSchemaError("controller decision action bundle is malformed") from exc
                    if (
                        decision_bundle.action_id != str(decision["action_id"])
                        or decision_bundle.decision_id != ControllerDecisionId(str(decision["decision_id"]))
                        or int(decision_bundle.generation) != int(decision["current_generation"])
                        or decision_bundle.sha256 != str(decision["action_bundle_sha256"])
                    ):
                        raise CorruptSchemaError("controller decision action bundle identity is conflicting")
                    if (
                        str(decision["state"]) == ControllerDecisionState.ACTION_COMMITTED.value
                        and str(outbox["state"]) != "committed"
                    ):
                        raise CorruptSchemaError("action-committed decision lacks a committed outbox")
                    if (
                        str(decision["state"]) == ControllerDecisionState.ACKNOWLEDGED.value
                        and str(outbox["state"]) != "acknowledged"
                    ):
                        raise CorruptSchemaError("acknowledged decision lacks an acknowledged outbox")
                    if decision["claimant_kind"] is not None and (
                        str(outbox["claimant_kind"]) != str(decision["claimant_kind"])
                        or str(outbox["claimant_id"]) != str(decision["claimant_id"])
                    ):
                        raise CorruptSchemaError("controller action claimant facts diverge")
                    inspected_generation = (
                        self._db()
                        .execute(
                            "SELECT inspection_outcome, inspection_bundle_sha256 FROM controller_decision_generations "
                            "WHERE decision_id = ? AND generation = ?",
                            (str(decision["decision_id"]), int(decision["current_generation"])),
                        )
                        .fetchone()
                    )
                    if (
                        inspected_generation is not None
                        and inspected_generation["inspection_outcome"] == (ControllerGenerationState.COMPLETED.value)
                        and str(inspected_generation["inspection_bundle_sha256"])
                        != str(decision["action_bundle_sha256"])
                    ):
                        raise CorruptSchemaError("controller inspected bundle and committed action diverge")
                if (
                    str(decision["state"])
                    not in {
                        ControllerDecisionState.ACKNOWLEDGED.value,
                        ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value,
                    }
                    and decision["acknowledged_at"] is not None
                ):
                    raise CorruptSchemaError("unacknowledged controller decision carries acknowledgement time")
                decision_state = str(decision["state"])
                if decision_state == ControllerDecisionState.CLAIMED.value:
                    if decision["claim_lease_expires_at"] is None:
                        raise CorruptSchemaError("claimed controller decision lacks claim lease expiry")
                elif decision["claim_lease_expires_at"] is not None:
                    raise CorruptSchemaError("non-claimed controller decision carries a live claim lease")
                if decision_state == ControllerDecisionState.SUPERSEDED.value:
                    if decision["superseded_at"] is None:
                        raise CorruptSchemaError("superseded controller decision lacks superseded timestamp")
                elif decision["superseded_at"] is not None:
                    raise CorruptSchemaError("non-superseded controller decision carries superseded timestamp")
                if decision_state == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value:
                    if (
                        not isinstance(decision["human_attention_reason"], str)
                        or not str(decision["human_attention_reason"]).strip()
                    ):
                        raise CorruptSchemaError("human-attention controller decision lacks a reason")
                elif decision["human_attention_reason"] is not None:
                    raise CorruptSchemaError("non-attention controller decision carries an attention reason")
                summary_dispatch = decoded_summary.get("dispatch_id")
                summary_kind = decoded_summary.get("kind")
                if summary_dispatch != str(decision["dispatch_id"]) or summary_kind != str(decision["kind"]):
                    raise CorruptSchemaError("controller decision summary identity is conflicting")
                if decoded_summary.get("cycle_sequence", int(decision["cycle_sequence"])) != int(
                    decision["cycle_sequence"]
                ):
                    raise CorruptSchemaError("controller decision summary cycle sequence is conflicting")
                successor_values = decoded_summary.get("expected_successor_dispatch_ids", [])
                if not isinstance(successor_values, list) or any(
                    not isinstance(item, str) for item in successor_values
                ):
                    raise CorruptSchemaError("controller successor snapshot is malformed")
                if successor_values != sorted(set(successor_values)):
                    raise CorruptSchemaError("controller successor snapshot is not canonical")
                if any(
                    not isinstance(item, str)
                    or self._db()
                    .execute(
                        "SELECT 1 FROM dispatch_queue WHERE dispatch_id = ?",
                        (item,),
                    )
                    .fetchone()
                    is None
                    for item in successor_values
                ):
                    raise CorruptSchemaError("controller successor snapshot points outside the queue")
                wake = (
                    self._db()
                    .execute("SELECT * FROM wake_outbox WHERE decision_id = ?", (str(decision["decision_id"]),))
                    .fetchone()
                )
                if wake is None and str(decision["state"]) not in {
                    ControllerDecisionState.ACTION_COMMITTED.value,
                    ControllerDecisionState.ACKNOWLEDGED.value,
                    ControllerDecisionState.SUPERSEDED.value,
                    ControllerDecisionState.LEGACY_CLOSED.value,
                }:
                    raise CorruptSchemaError("controller decision lacks its wake delivery")
                if wake is not None:
                    if not 1 <= int(wake["cycle_sequence"]) <= 64:
                        raise CorruptSchemaError("controller wake cycle sequence is invalid")
                    expected_decision_id = f"decision/{wake['delivery_id']}"
                    if str(decision["decision_id"]) != expected_decision_id:
                        raise CorruptSchemaError("controller decision identity is not bound to its wake")
                    if (
                        str(wake["state"]) == "pending"
                        and str(decision["state"]) != ControllerDecisionState.PENDING_DELIVERY.value
                    ):
                        raise CorruptSchemaError("pending wake has a non-pending controller decision")
                    if str(wake["dispatch_id"]) != str(decision["dispatch_id"]) or str(wake["kind"]) != str(
                        decision["kind"]
                    ):
                        raise CorruptSchemaError("controller wake identity is conflicting")
                    if int(wake["cycle_sequence"]) != int(decision["cycle_sequence"]):
                        raise CorruptSchemaError("controller wake cycle sequence is conflicting")
                    if str(wake["state"]) == "suppressed" and wake["source_turn_id"] is not None:
                        raise CorruptSchemaError("suppressed controller wake carries a source turn")
                    if str(decision["state"]) == ControllerDecisionState.PENDING_DELIVERY.value and str(
                        wake["state"]
                    ) not in {"pending", "starting", "failed", "ambiguous"}:
                        raise CorruptSchemaError("pending-delivery controller decision lacks a pending wake")
                    if str(decision["state"]) == ControllerDecisionState.AWAITING_CLAIM.value and str(
                        wake["state"]
                    ) not in {"delivered", "not_applicable", "suppressed", "failed"}:
                        raise CorruptSchemaError("awaiting-claim controller decision has an undelivered wake")

            generation_rows = (
                self._db()
                .execute("SELECT * FROM controller_decision_generations ORDER BY decision_id, generation")
                .fetchall()
            )
            generations_by_decision: dict[str, list[sqlite3.Row]] = {}
            for generation in generation_rows:
                generations_by_decision.setdefault(str(generation["decision_id"]), []).append(generation)
                try:
                    ControllerGenerationState(str(generation["state"]))
                    Generation(generation["generation"])
                except ValueError as exc:
                    raise CorruptSchemaError("controller generation state is malformed") from exc
                if generation["controller_thread_id"] is not None:
                    ThreadIdentity(str(generation["controller_thread_id"]))
                if (
                    generation["inspection_token_sha256"] is not None
                    and re.fullmatch(r"[0-9a-f]{64}", str(generation["inspection_token_sha256"])) is None
                ):
                    raise CorruptSchemaError("controller inspection token digest is malformed")
                if (
                    generation["inspection_lease_expires_at"] is not None
                    and generation["inspection_token_sha256"] is None
                ):
                    raise CorruptSchemaError("controller inspection lease lacks its capability digest")
                if generation["inspection_completed_at"] is not None and generation["inspection_started_at"] is None:
                    raise CorruptSchemaError("controller inspection timestamps are incomplete")
                if (
                    generation["inspection_completed_at"] is not None
                    and generation["inspection_lease_expires_at"] is not None
                ):
                    raise CorruptSchemaError("completed controller inspection retains a live lease")
                if (
                    generation["inspection_started_at"] is not None
                    and generation["inspection_completed_at"] is None
                    and generation["inspection_lease_expires_at"] is None
                ):
                    raise CorruptSchemaError("pending controller inspection lacks its lease")
                if generation["inspection_completed_at"] is not None and generation["inspection_outcome"] is None:
                    raise CorruptSchemaError("completed controller inspection lacks its outcome")
                if generation["inspection_outcome"] is not None:
                    try:
                        inspection_state = ControllerGenerationState(str(generation["inspection_outcome"]))
                    except ValueError as exc:
                        raise CorruptSchemaError("controller inspection outcome is unknown") from exc
                    if generation["inspection_completed_at"] is None:
                        raise CorruptSchemaError("controller inspection outcome lacks completion timestamp")
                    if inspection_state in {
                        ControllerGenerationState.PREPARED,
                        ControllerGenerationState.DELIVERY_STARTING,
                        ControllerGenerationState.SUPERSEDED,
                    }:
                        raise CorruptSchemaError("controller inspection outcome is not terminal evidence")
                    if str(generation["state"]) != inspection_state.value:
                        raise CorruptSchemaError("controller generation state and inspection outcome diverge")
                    if inspection_state is ControllerGenerationState.COMPLETED:
                        if (
                            generation["inspection_bundle_json"] is None
                            or generation["inspection_bundle_sha256"] is None
                        ):
                            raise CorruptSchemaError("completed controller inspection lacks its canonical bundle")
                        decision_for_audit = next(
                            (
                                item
                                for item in decision_rows
                                if str(item["decision_id"]) == str(generation["decision_id"])
                            ),
                            None,
                        )
                        if decision_for_audit is None or str(generation["inspection_token_sha256"]) != (
                            self._controller_completed_inspection_claimant_audit_sha256(
                                decision_for_audit,
                                generation,
                            )
                        ):
                            raise CorruptSchemaError("completed controller inspection claimant audit is invalid")
                    elif (
                        generation["inspection_bundle_json"] is not None
                        or generation["inspection_bundle_sha256"] is not None
                    ):
                        raise CorruptSchemaError("non-completed controller inspection carries an action bundle")
                elif (
                    generation["inspection_bundle_json"] is not None
                    or generation["inspection_bundle_sha256"] is not None
                ):
                    raise CorruptSchemaError("controller inspection bundle lacks its outcome")
                if generation["inspection_bundle_json"] is not None:
                    bundle_text = str(generation["inspection_bundle_json"])
                    if hashlib.sha256(bundle_text.encode("utf-8")).hexdigest() != str(
                        generation["inspection_bundle_sha256"]
                    ):
                        raise CorruptSchemaError("controller inspected bundle digest is invalid")
                    try:
                        inspected_bundle = ModelFacingControllerActionBundle.from_json_bytes(bundle_text)
                    except (TypeError, ValueError) as exc:
                        raise CorruptSchemaError("controller inspected bundle is malformed") from exc
                    if (
                        inspected_bundle.decision_id != ControllerDecisionId(str(generation["decision_id"]))
                        or int(inspected_bundle.generation) != int(generation["generation"])
                        or inspected_bundle.sha256 != str(generation["inspection_bundle_sha256"])
                    ):
                        raise CorruptSchemaError("controller inspected bundle identity is conflicting")
                if generation["controller_turn_id"] is not None and generation["controller_thread_id"] is None:
                    raise CorruptSchemaError("controller turn identity lacks its thread identity")
                generation_state = str(generation["state"])
                if generation_state == ControllerGenerationState.ACTIVE.value and (
                    generation["controller_thread_id"] is None or generation["started_at"] is None
                ):
                    raise CorruptSchemaError("active controller generation lacks start identity")
                if (
                    generation_state
                    in {
                        ControllerGenerationState.COMPLETED.value,
                        ControllerGenerationState.FAILED.value,
                        ControllerGenerationState.INTERRUPTED.value,
                        ControllerGenerationState.AMBIGUOUS.value,
                        ControllerGenerationState.UNAVAILABLE.value,
                    }
                    and generation["terminal_at"] is None
                ):
                    raise CorruptSchemaError("terminal controller generation lacks terminal time")
                if (
                    generation_state
                    in {
                        ControllerGenerationState.PREPARED.value,
                        ControllerGenerationState.DELIVERY_STARTING.value,
                        ControllerGenerationState.ACTIVE.value,
                    }
                    and generation["terminal_at"] is not None
                ):
                    raise CorruptSchemaError("nonterminal controller generation carries terminal time")
                if generation["source_kind"] == "replacement_controller" and generation["predecessor_generation"] != 1:
                    raise CorruptSchemaError("replacement controller generation lacks predecessor")
            for decision in decision_rows:
                generations = generations_by_decision.get(str(decision["decision_id"]), [])
                expected_generations = list(range(1, int(decision["current_generation"]) + 1))
                if [int(item["generation"]) for item in generations] != expected_generations:
                    raise CorruptSchemaError("controller generation lineage is discontinuous")
                if generations and str(generations[0]["source_kind"]) != "source_controller":
                    raise CorruptSchemaError("controller source generation kind is invalid")
                if len(generations) == 2 and (
                    str(generations[1]["source_kind"]) != "replacement_controller"
                    or generations[1]["predecessor_generation"] != 1
                ):
                    raise CorruptSchemaError("controller replacement lineage is invalid")
            action_rows = self._db().execute("SELECT * FROM controller_action_outbox ORDER BY action_id").fetchall()
            decision_by_id = {str(item["decision_id"]): item for item in decision_rows}
            for action in action_rows:
                decision = decision_by_id.get(str(action["decision_id"]))
                if decision is None:
                    raise CorruptSchemaError("controller action outbox decision is missing")
                is_program_action = has_program_decisions and decision["program_id"] is not None
                try:
                    ControllerDecisionId(str(action["decision_id"]))
                    ControllerClaimantKind(str(action["claimant_kind"]))
                    if is_program_action:
                        ModelFacingProgramControllerActionBundle.from_json_bytes(str(action["bundle_json"]))
                    else:
                        ModelFacingControllerActionBundle.from_json_bytes(str(action["bundle_json"]))
                except (TypeError, ValueError) as exc:
                    raise CorruptSchemaError("controller action outbox bundle is malformed") from exc
                if hashlib.sha256(str(action["bundle_json"]).encode("utf-8")).hexdigest() != action["bundle_sha256"]:
                    raise CorruptSchemaError("controller action bundle digest is invalid")
                if (
                    hashlib.sha256(str(action["effect_receipt_json"]).encode("utf-8")).hexdigest()
                    != action["effect_receipt_sha256"]
                ):
                    raise CorruptSchemaError("controller effect receipt digest is invalid")
                try:
                    effect_receipt = strict_json_loads(str(action["effect_receipt_json"]), max_bytes=32 * 1024)
                except ValueError as exc:
                    raise CorruptSchemaError("controller effect receipt is malformed") from exc
                bundle = (
                    ModelFacingProgramControllerActionBundle.from_json_bytes(str(action["bundle_json"]))
                    if is_program_action
                    else ModelFacingControllerActionBundle.from_json_bytes(str(action["bundle_json"]))
                )
                bundle_revision = bundle.expected_program_revision if is_program_action else bundle.expected_revision
                if (
                    bundle.action_id != str(action["action_id"])
                    or bundle.decision_id != ControllerDecisionId(str(action["decision_id"]))
                    or int(bundle.generation) != int(action["generation"])
                    or int(bundle_revision)
                    != int(decision["program_revision"] if is_program_action else action["expected_revision"])
                ):
                    raise CorruptSchemaError("controller action outbox identity is conflicting")
                if is_program_action:
                    expected_applied_actions = [
                        (
                            f"start:{','.join(item.milestone_ids)}"
                            if item.kind is ProgramControllerActionKind.START_READY_MILESTONES
                            else f"review:{item.milestone_id}"
                            if item.kind is ProgramControllerActionKind.START_REVIEWS
                            else f"repair:{item.milestone_id}"
                            if item.kind is ProgramControllerActionKind.REQUEST_REPAIR
                            else f"resolve-blocker:{item.milestone_id}:{item.blocker_gate_id}"
                            if item.kind is ProgramControllerActionKind.RESOLVE_CANDIDATE_BLOCKER
                            else f"promote:{item.milestone_id}"
                            if item.kind is ProgramControllerActionKind.PROMOTE_CANDIDATE
                            else f"integrate:{item.milestone_id}"
                            if item.kind is ProgramControllerActionKind.INTEGRATE_CANDIDATE
                            else item.kind.value
                        )
                        for item in bundle.actions
                    ]
                else:
                    expected_applied_actions = [item.kind.value for item in bundle.actions]
                expected_effect_receipt: JsonObject = {
                    "action_id": bundle.action_id,
                    "decision_id": str(bundle.decision_id),
                    "generation": int(bundle.generation),
                    "bundle_sha256": bundle.sha256,
                    "applied_actions": expected_applied_actions,
                    **(
                        {"program_revision": int(decision["program_revision"]) + 1}
                        if is_program_action
                        else {"applied_revision": int(action["expected_revision"]) + 1}
                    ),
                }
                if is_program_action and "external_effect_ids" in effect_receipt:
                    expected_effect_receipt["external_effect_ids"] = [
                        effect_id for effect_id, _milestone_id in bundle.external_effects
                    ]
                if (
                    not isinstance(effect_receipt, dict)
                    or str(action["effect_receipt_json"]) != _encode_json(effect_receipt)
                    or _encode_json(effect_receipt) != _encode_json(expected_effect_receipt)
                ):
                    raise CorruptSchemaError("controller effect receipt diverges from its committed bundle")
                generation = (
                    self._db()
                    .execute(
                        "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                        (str(action["decision_id"]), int(action["generation"])),
                    )
                    .fetchone()
                )
                if generation is None:
                    raise CorruptSchemaError("controller action outbox generation is missing")
                if str(action["state"]) == "acknowledged" and action["acknowledged_at"] is None:
                    raise CorruptSchemaError("acknowledged controller action lacks acknowledgement time")
                if str(action["state"]) == "committed" and action["acknowledged_at"] is not None:
                    raise CorruptSchemaError("committed controller action carries acknowledgement time")
                if (
                    str(decision["state"])
                    in {
                        ControllerDecisionState.ACTION_COMMITTED.value,
                        ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value,
                    }
                    and str(action["state"]) != "committed"
                    and not (
                        str(decision["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                        and str(action["state"]) == "acknowledged"
                    )
                ):
                    raise CorruptSchemaError("action-committed controller decision has an invalid receipt state")
                if str(decision["action_id"]) != str(action["action_id"]):
                    raise CorruptSchemaError("controller decision action identity is conflicting")
                if (
                    decision["action_bundle_sha256"] != action["bundle_sha256"]
                    or decision["action_bundle_json"] != action["bundle_json"]
                ):
                    raise CorruptSchemaError("controller decision action bundle is conflicting")
                if decision["acknowledged_at"] != action["acknowledged_at"]:
                    raise CorruptSchemaError("controller acknowledgement facts are conflicting")
                expected_revision = int(action["expected_revision"]) + 1
                if int(decision["revision"]) != expected_revision:
                    raise CorruptSchemaError("controller decision revision is conflicting")
                if decision["committed_at"] != action["committed_at"]:
                    raise CorruptSchemaError("controller commit timestamps are conflicting")
                decision_claimant = decision["claimant_kind"]
                if decision_claimant is None or decision["claimant_id"] is None:
                    raise CorruptSchemaError("controller action lacks its claimant audit")
                if str(action["claimant_kind"]) != str(decision_claimant) or str(action["claimant_id"]) != str(
                    decision["claimant_id"]
                ):
                    raise CorruptSchemaError("controller action claimant is conflicting")
                if generation["inspection_bundle_json"] is not None and (
                    generation["inspection_bundle_json"] != action["bundle_json"]
                    or generation["inspection_bundle_sha256"] != action["bundle_sha256"]
                ):
                    raise CorruptSchemaError("controller action differs from the inspected bundle")
                if (
                    str(decision["state"]) == ControllerDecisionState.ACKNOWLEDGED.value
                    and str(action["state"]) != "acknowledged"
                ):
                    raise CorruptSchemaError("acknowledged controller decision has an unacknowledged receipt")
                if str(decision["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value and action[
                    "state"
                ] not in {
                    "committed",
                    "acknowledged",
                }:
                    raise CorruptSchemaError("human-attention controller decision has an invalid receipt state")

        if any(int(self._db().execute(query).fetchone()[0]) for query in orphan_queries):
            raise CorruptSchemaError("workflow ledger contains orphan rows")
        dispatch_duplicate_columns = (
            "run_id, milestone_id, role, generation"
            if self._has_complete_program_schema()
            else "run_id, milestone_id, role"
        )
        duplicate_queries = (
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id FROM milestones GROUP BY run_id, milestone_id HAVING COUNT(*) > 1)",
            f"SELECT COUNT(*) FROM (SELECT {dispatch_duplicate_columns} FROM dispatches GROUP BY {dispatch_duplicate_columns} HAVING COUNT(*) > 1)",
            "SELECT COUNT(*) FROM (SELECT run_id, milestone_id, sequence FROM events GROUP BY run_id, milestone_id, sequence HAVING COUNT(*) > 1)",
        )
        if any(int(self._db().execute(query).fetchone()[0]) for query in duplicate_queries):
            raise CorruptSchemaError("workflow ledger contains duplicate logical rows")
        run_rows = self._db().execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        for row in run_rows:
            RunId(row["run_id"])
            _decode_object(row["metadata_json"], field_name="run metadata")
        milestone_rows = self._db().execute("SELECT * FROM milestones ORDER BY run_id, milestone_id").fetchall()
        milestone_keys = {(str(row["run_id"]), str(row["milestone_id"])) for row in milestone_rows}
        dispatch_rows = self._db().execute("SELECT * FROM dispatches ORDER BY run_id, milestone_id, role").fetchall()
        self._fault("after_dispatch_rows_read")
        dispatch_keys = set()
        dispatches_by_id: dict[DispatchId, DispatchClaim] = {}
        for row in dispatch_rows:
            key = (
                (str(row["run_id"]), str(row["milestone_id"]), str(row["role"]), int(row["generation"]))
                if self._has_complete_program_schema()
                else (str(row["run_id"]), str(row["milestone_id"]), str(row["role"]))
            )
            if key in dispatch_keys:
                raise CorruptSchemaError("duplicate dispatch ownership")
            dispatch_keys.add(key)
            dispatch = self._dispatch_from_row(row)
            if dispatch.dispatch_id != DispatchId.from_parts(
                dispatch.run_id, dispatch.milestone_id, dispatch.role, dispatch.generation
            ):
                raise CorruptSchemaError("dispatch identity does not match its columns")
            if (str(dispatch.run_id), str(dispatch.milestone_id)) not in milestone_keys:
                raise CorruptSchemaError("dispatch points outside its milestone")
            dispatches_by_id[dispatch.dispatch_id] = dispatch
        event_rows = self._db().execute("SELECT * FROM events ORDER BY run_id, milestone_id, sequence").fetchall()
        events_by_milestone: dict[tuple[str, str], list[EventRecord]] = {}
        # Executor dispatches own milestone lifecycle transitions and therefore
        # require one causal claim event.  Program reviewer dispatches are
        # historical audit rows only; they may have a legacy low-level claim
        # event, but program review claims do not reopen or transition the
        # milestone and consequently may have no event to replay.
        claim_counts = dict.fromkeys(dispatches_by_id, 0)
        for row in event_rows:
            event = self._event_from_row(row)
            try:
                _event_type(event.event_type)
            except ValueError as exc:
                raise CorruptSchemaError("event has an unsupported kind") from exc
            if event.event_id != EventId(f"{event.run_id}/{event.milestone_id}/{int(event.sequence)}"):
                raise CorruptSchemaError("event identity does not match its columns")
            events_by_milestone.setdefault((str(event.run_id), str(event.milestone_id)), []).append(event)
            if event.from_state is None or not is_transition_allowed(event.from_state, event.to_state):
                raise CorruptSchemaError("event contains a forbidden state transition")
            if event.event_type == "dispatch_claimed":
                if (
                    event.dispatch_id is None
                    or event.reason is None
                    or event.reason.code is not ReasonCode.DISPATCH_CLAIMED
                ):
                    raise CorruptSchemaError("dispatch claim event has an invalid causal contract")
                dispatch = dispatches_by_id.get(event.dispatch_id)
                if dispatch is None or (dispatch.run_id, dispatch.milestone_id) != (
                    event.run_id,
                    event.milestone_id,
                ):
                    raise CorruptSchemaError("dispatch claim event does not match its dispatch row")
                initial_claim = (
                    dispatch.generation == Generation(1)
                    and event.from_state is WorkflowState.PLANNED
                    and event.to_state is WorkflowState.STARTING
                )
                repair_claim = (
                    dispatch.role == RoleId("executor")
                    and dispatch.generation > Generation(1)
                    and event.from_state is WorkflowState.REPAIR_REQUIRED
                    and event.to_state is WorkflowState.STARTING
                )
                if not (initial_claim or repair_claim):
                    raise CorruptSchemaError("dispatch claim event has an invalid causal contract")
                claim_counts[event.dispatch_id] += 1
            elif event.dispatch_id is not None or (
                event.reason is not None and event.reason.code is ReasonCode.DISPATCH_CLAIMED
            ):
                raise CorruptSchemaError("normal state transition carries dispatch authority")
            elif event.from_state is WorkflowState.PLANNED and event.to_state is WorkflowState.STARTING:
                raise CorruptSchemaError("PLANNED -> STARTING requires a dispatch claim event")
        for dispatch_id, count in claim_counts.items():
            dispatch = dispatches_by_id[dispatch_id]
            if (dispatch.role == RoleId("executor") and count != 1) or (
                dispatch.role != RoleId("executor") and count > 1
            ):
                raise CorruptSchemaError("dispatch claim event count violates its role contract")
        for row in milestone_rows:
            key = (str(row["run_id"]), str(row["milestone_id"]))
            milestone = self._milestone_from_row(row)
            history = events_by_milestone.get(key, [])
            sequences = tuple(int(event.sequence) for event in history)
            if sequences != tuple(range(1, len(history) + 1)):
                raise CorruptSchemaError("milestone event sequence is not contiguous")
            replay_state = WorkflowState.PLANNED
            for event in history:
                if event.from_state is not replay_state:
                    raise CorruptSchemaError("milestone event history is non-causal")
                replay_state = event.to_state
            if replay_state is not milestone.state:
                raise CorruptSchemaError("milestone state does not match replayed history")
        if has_execution_tables:
            lease_rows = self._db().execute("SELECT * FROM workspace_leases ORDER BY workspace_path").fetchall()
            leases = {str(row["workspace_path"]): self._workspace_lease_from_row(row) for row in lease_rows}
            integrity_keys = (
                {
                    (str(row["run_id"]), str(row["milestone_id"]))
                    for row in self._db().execute("SELECT run_id, milestone_id FROM execution_integrity").fetchall()
                }
                if has_integrity
                else set()
            )
            app_native_by_execution: dict[tuple[str, str], AppNativeDispatchRecord] = {}
            if has_app_native:
                for app_row in (
                    self._db().execute("SELECT * FROM app_native_dispatches ORDER BY run_id, milestone_id").fetchall()
                ):
                    app_record = self._app_native_from_row(app_row)
                    dispatch = dispatches_by_id.get(app_record.action.dispatch_id)
                    if dispatch is None or dispatch.role != RoleId("executor"):
                        raise CorruptSchemaError("App-native action lacks its executor dispatch authority")
                    key = (str(dispatch.run_id), str(dispatch.milestone_id))
                    if key != (str(app_row["run_id"]), str(app_row["milestone_id"])):
                        raise CorruptSchemaError("App-native action does not match its execution identity")
                    if key in app_native_by_execution:
                        raise CorruptSchemaError("execution has duplicate App-native ownership")
                    app_native_by_execution[key] = app_record
            execution_rows = self._db().execute("SELECT * FROM executions ORDER BY run_id, milestone_id").fetchall()
            for row in execution_rows:
                execution = self._execution_from_row(row)
                execution_key = (str(execution.run_id), str(execution.milestone_id))
                app_native = app_native_by_execution.get(execution_key)
                if app_native is not None:
                    app_lease = leases.get(str(app_native.action.workspace_path))
                    if (
                        app_native.action.workspace_path != execution.workspace_path
                        or app_lease is None
                        or app_lease.owner_run_id != execution.run_id
                    ):
                        raise CorruptSchemaError("App-native action workspace does not match execution/lease authority")
                if app_native is not None and (
                    (app_native.identity is None) != (execution.thread_id is None)
                    or (app_native.identity is not None and app_native.identity.thread_id != execution.thread_id)
                ):
                    raise CorruptSchemaError("App-native host/thread identity does not match its execution")
                if (
                    has_integrity
                    and execution.status not in {ExecutionStatus.PLANNED, ExecutionStatus.CANCELLED}
                    and execution_key not in integrity_keys
                    and app_native is None
                ):
                    raise CorruptSchemaError("non-planned execution lacks its integrity authority row")
                milestone_state = _row_state(
                    self._db()
                    .execute(
                        "SELECT current_state FROM milestones WHERE run_id = ? AND milestone_id = ?",
                        (str(execution.run_id), str(execution.milestone_id)),
                    )
                    .fetchone()[0]
                )
                if execution.status not in {ExecutionStatus.PLANNED, ExecutionStatus.CANCELLED}:
                    lease = leases.get(str(execution.workspace_path))
                    if lease is None or lease.owner_run_id != execution.run_id:
                        raise CorruptSchemaError("active execution has no matching workspace lease")
                if execution.status is ExecutionStatus.PLANNED:
                    if (
                        execution.checkpoint
                        not in {
                            ControllerCheckpoint.CAPSULE_PLANNED,
                            ControllerCheckpoint.WORKSPACE_LEASED,
                            ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE,
                            ControllerCheckpoint.THREAD_STARTING,
                        }
                        or execution.thread_id
                    ):
                        raise CorruptSchemaError("planned execution has post-plan authority")
                elif execution.status is ExecutionStatus.THREAD_STARTED:
                    if execution.thread_id is None or execution.checkpoint not in {
                        ControllerCheckpoint.THREAD_IDENTITY_DURABLE,
                        ControllerCheckpoint.TURN_DURABLE,
                    }:
                        raise CorruptSchemaError("started execution has incomplete thread authority")
                    if milestone_state is not WorkflowState.RUNNING:
                        raise CorruptSchemaError("started execution is not paired with RUNNING workflow state")
                    if app_native is not None and (
                        app_native.state is not AppNativeState.BOUND
                        or app_native.identity is None
                        or app_native.identity.thread_id != execution.thread_id
                    ):
                        raise CorruptSchemaError("started App-native execution has mismatched host identity")
                elif execution.status is ExecutionStatus.COMPLETED:
                    if (
                        execution.checkpoint is not ControllerCheckpoint.RESULT_DURABLE
                        or execution.thread_id is None
                        or (execution.turn_id is None and app_native is None)
                        or execution.result is None
                        or execution.validation is None
                        or execution.protected_after_sha256 is None
                    ):
                        raise CorruptSchemaError("completed execution is missing terminal evidence")
                    if milestone_state not in {
                        WorkflowState.COMPLETED,
                        WorkflowState.REVIEWING,
                        WorkflowState.ACCEPTED,
                        WorkflowState.FAILED,
                    }:
                        raise CorruptSchemaError(
                            "completed execution is not paired with a valid post-execution workflow state"
                        )
                    if app_native is not None and (
                        app_native.state is not AppNativeState.COMPLETED
                        or app_native.result is None
                        or app_native.result.to_json() != execution.result
                    ):
                        raise CorruptSchemaError("completed App-native execution has mismatched terminal result")
                elif execution.status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY and execution.thread_id is not None:
                    raise CorruptSchemaError("pre-identity uncertainty cannot carry a thread identity")
                elif execution.status is ExecutionStatus.FAILED and (
                    execution.checkpoint is not ControllerCheckpoint.RESULT_DURABLE
                    or execution.result is None
                    or execution.protected_after_sha256 is None
                ):
                    raise CorruptSchemaError("failed execution is missing terminal evidence")
                if (
                    execution.status is ExecutionStatus.FAILED
                    and app_native is not None
                    and (
                        app_native.state is not AppNativeState.FAILED
                        or app_native.result is None
                        or app_native.result.to_json() != execution.result
                    )
                ):
                    raise CorruptSchemaError("failed App-native execution has mismatched terminal result")
                if execution.status is ExecutionStatus.FAILED and milestone_state is not WorkflowState.FAILED:
                    raise CorruptSchemaError("failed execution is not paired with FAILED workflow state")
                if execution.status is ExecutionStatus.CANCELLED and milestone_state is not WorkflowState.CANCELLED:
                    raise CorruptSchemaError("cancelled execution is not paired with CANCELLED workflow state")
                if (
                    execution.status is ExecutionStatus.CANCELLED
                    and app_native is not None
                    and (app_native.state is not AppNativeState.CANCELLED or app_native.result is not None)
                ):
                    raise CorruptSchemaError("cancelled execution has nonterminal App-native recovery state")
                if (
                    execution.status is ExecutionStatus.UNCERTAIN_PRE_IDENTITY
                    and milestone_state is not WorkflowState.STARTING
                ):
                    raise CorruptSchemaError("pre-identity uncertainty is not paired with STARTING workflow state")
                event_rows = (
                    self._db()
                    .execute(
                        "SELECT sequence FROM sdk_lifecycle_events WHERE run_id = ? AND milestone_id = ? "
                        "ORDER BY turn_id, sequence",
                        (str(execution.run_id), str(execution.milestone_id)),
                    )
                    .fetchall()
                )
                if execution.turn_id is not None and event_rows:
                    sequences = tuple(int(item[0]) for item in event_rows)
                    if sequences != tuple(range(len(sequences))):
                        raise CorruptSchemaError("SDK lifecycle sequence is not contiguous")
                if has_causal_workspace and execution_key in integrity_keys:
                    integrity = self.get_execution_integrity(execution.run_id, execution.milestone_id)
                    if integrity.provenance in {"controller_v4", "controller_v5"} and (
                        integrity.workspace_baseline_head_sha is None or integrity.workspace_baseline is None
                    ):
                        raise CorruptSchemaError("controller execution lacks its workspace baseline authority")
                    if integrity.provenance == "controller_v5" and integrity.git_authority_before_sha256 is None:
                        raise CorruptSchemaError("controller execution lacks its pre-external Git authority")
                    if (
                        execution.status is ExecutionStatus.COMPLETED
                        and integrity.provenance == "controller_v5"
                        and (
                            integrity.workspace_terminal_head_sha is None
                            or integrity.workspace_terminal is None
                            or integrity.workspace_terminal_sha256 is None
                        )
                    ):
                        raise CorruptSchemaError("completed controller execution lacks terminal workspace authority")
                    if execution.turn_id is not None and integrity.turn_started_at is None:
                        raise CorruptSchemaError("durable SDK turn lacks causal turn-start authority")
                    if (
                        execution.turn_id is None
                        and integrity.turn_started_at is not None
                        and (execution.turn_output != _TURN_STARTING_MARKER)
                    ):
                        raise CorruptSchemaError("turn-start authority lacks its active external-call marker")
                    if execution.turn_output == _TURN_STARTING_MARKER and integrity.turn_started_at is None:
                        raise CorruptSchemaError("turn-start marker lacks durable causal authority")
            if has_integrity:
                for row in (
                    self._db().execute("SELECT * FROM execution_integrity ORDER BY run_id, milestone_id").fetchall()
                ):
                    integrity = self._execution_integrity_from_row(row)
                    if (
                        integrity.git_authority_after_sha256 is not None
                        and integrity.git_authority_before_sha256 is None
                    ):
                        raise CorruptSchemaError("Git authority after-state lacks its pre-turn authority")

    def _validate_program_decision_row(self, decision: sqlite3.Row) -> None:
        """Validate a program decision against the shared controller tables."""

        try:
            program_id = ProgramId(str(decision["program_id"]))
            event_kind = ProgramEventKind(str(decision["event_kind"]))
            decision_id = ControllerDecisionId(str(decision["decision_id"]))
            state = ControllerDecisionState(str(decision["state"]))
        except ValueError as exc:
            raise CorruptSchemaError("program controller decision identity or state is malformed") from exc
        if decision["dispatch_id"] is not None or decision["program_revision"] is None:
            raise CorruptSchemaError("program controller decision has a conflicting subject")
        event_key = decision["event_key"]
        if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 256:
            raise CorruptSchemaError("program controller event key is malformed")
        if str(decision_id) != f"decision/program/{program_id}/{event_kind.value}/{event_key}":
            raise CorruptSchemaError("program controller decision identity is not canonical")
        try:
            summary = strict_json_loads(str(decision["summary_json"]), max_bytes=16_384)
        except ValueError as exc:
            raise CorruptSchemaError("program controller decision summary is invalid") from exc
        if not isinstance(summary, dict) or hashlib.sha256(
            str(decision["summary_json"]).encode("utf-8")
        ).hexdigest() != str(decision["summary_sha256"]):
            raise CorruptSchemaError("program controller decision summary digest is invalid")
        if (
            summary.get("program_id") != str(program_id)
            or summary.get("event_kind") != event_kind.value
            or summary.get("event_key") != event_key
            or summary.get("program_revision") != int(decision["program_revision"])
        ):
            raise CorruptSchemaError("program controller decision summary identity is conflicting")
        claim_fields = tuple(
            decision[name] for name in ("claimant_kind", "claimant_id", "claim_token_sha256", "claim_started_at")
        )
        if any(item is None for item in claim_fields) and not all(item is None for item in claim_fields):
            raise CorruptSchemaError("program controller claim facts are incomplete")
        if decision["claimant_kind"] is not None:
            try:
                ControllerClaimantKind(str(decision["claimant_kind"]))
            except ValueError as exc:
                raise CorruptSchemaError("program controller claimant kind is invalid") from exc
        action_fields = tuple(
            decision[name] for name in ("action_id", "action_bundle_json", "action_bundle_sha256", "committed_at")
        )
        if any(item is None for item in action_fields) and not all(item is None for item in action_fields):
            raise CorruptSchemaError("program controller action facts are incomplete")
        if state is ControllerDecisionState.CLAIMED and (
            decision["claim_token_sha256"] is None or decision["claim_lease_expires_at"] is None
        ):
            raise CorruptSchemaError("claimed program controller decision lacks lease facts")
        if state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED and not isinstance(
            decision["human_attention_reason"], str
        ):
            raise CorruptSchemaError("human-attention program controller decision lacks a reason")
        if (
            state is not ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
            and decision["human_attention_reason"] is not None
        ):
            raise CorruptSchemaError("program controller decision carries an unexpected attention reason")
        if state in {ControllerDecisionState.ACTION_COMMITTED, ControllerDecisionState.ACKNOWLEDGED} and any(
            item is None for item in action_fields
        ):
            raise CorruptSchemaError("committed program controller decision lacks action facts")
        if decision["action_id"] is not None:
            try:
                bundle = ModelFacingProgramControllerActionBundle.from_json_bytes(str(decision["action_bundle_json"]))
            except (TypeError, ValueError) as exc:
                raise CorruptSchemaError("program controller action bundle is malformed") from exc
            if (
                bundle.decision_id != decision_id
                or bundle.program_id != program_id
                or int(bundle.generation) != int(decision["current_generation"])
                or bundle.sha256 != str(decision["action_bundle_sha256"])
            ):
                raise CorruptSchemaError("program controller action identity is conflicting")
            outbox = (
                self._db()
                .execute(
                    "SELECT bundle_sha256, state FROM controller_action_outbox WHERE action_id = ?",
                    (str(decision["action_id"]),),
                )
                .fetchone()
            )
            if outbox is None or str(outbox["bundle_sha256"]) != str(decision["action_bundle_sha256"]):
                raise CorruptSchemaError("program controller action receipt is inconsistent")
            outbox_state = str(outbox["state"])
            if state is ControllerDecisionState.ACKNOWLEDGED:
                expected_states = {"acknowledged"}
            elif state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED:
                # A failed program effect owns the attention decision but is
                # still terminal.  Its all-terminal action outbox is closed
                # atomically, so the attention state may legitimately pair
                # with an acknowledged outbox.  Pending effects retain the
                # committed outbox until the final effect fact arrives.
                lifecycle_store_attached = any(
                    str(database[1]) == "h4" for database in self._db().execute("PRAGMA database_list").fetchall()
                )
                if lifecycle_store_attached:
                    pending_effect = any(
                        self.program_action_effect_state(bundle.action_id, effect_id) == "pending"
                        for effect_id, _milestone_id in bundle.external_effects
                    )
                    expected_states = {"committed"} if pending_effect else {"acknowledged"}
                else:
                    # The main ledger is validated before its existing H4
                    # sidecar is attached during startup.  Defer this
                    # cross-store check until the next transaction.
                    expected_states = {"committed", "acknowledged"}
            else:
                expected_states = {"committed"}
            if outbox_state not in expected_states:
                raise CorruptSchemaError("program controller action receipt is inconsistent")

    def _migrate(self, version: SchemaVersion) -> None:
        if version == SchemaVersion(18):
            self._migrate_v18_to_v19()
            return
        if version == SchemaVersion(17):
            self._migrate_v17_to_v18()
            return
        if version == SchemaVersion(16):
            self._migrate_v16_to_v17()
            return
        if version == SchemaVersion(15):
            self._migrate_v15_to_v16()
            return
        if version == SchemaVersion(14):
            self._migrate_v14_to_v15()
            return
        if version == SchemaVersion(13):
            self._migrate_v13_to_v14()
            return
        if version == SchemaVersion(12):
            self._migrate_v12_to_v13()
            return
        if version == SchemaVersion(11):
            self._migrate_v11_to_v12()
            return
        if version == SchemaVersion(10):
            self._migrate_v10_to_v11()
            return
        if version == SchemaVersion(9):
            self._migrate_v9_to_v10()
            return
        if version == SchemaVersion(8):
            self._migrate_v8_to_v9()
            return
        if version == SchemaVersion(7):
            self._migrate_v7_to_v8()
            return
        if version == SchemaVersion(6):
            self._migrate_v6_to_v7()
            return
        if version == SchemaVersion(5):
            self._migrate_v5_to_v6()
            return
        if version == SchemaVersion(4):
            self._migrate_v4_to_v5()
            return
        if version == SchemaVersion(3):
            self._migrate_v3_to_v4()
            return
        if version == SchemaVersion(2):
            self._migrate_v2_to_v3()
            return
        if version != SchemaVersion(1):
            raise UnsupportedSchemaVersion(f"ledger schema {version} has no owned migration")
        self._validate_shape(version)
        self._validate_rows()
        rows = {
            "runs": [tuple(row) for row in self._db().execute("SELECT run_id, created_at, metadata_json FROM runs")],
            "milestones": [tuple(row) for row in self._db().execute("SELECT * FROM milestones")],
            "dispatches": [tuple(row) for row in self._db().execute("SELECT * FROM dispatches")],
            "events": [tuple(row) for row in self._db().execute("SELECT * FROM events")],
        }
        self._db().execute("PRAGMA foreign_keys = OFF")
        try:
            with self._transaction(validate_authority=False):
                for table in ("events", "dispatches", "milestones", "runs", "schema_meta"):
                    self._db().execute(f"DROP TABLE {table}")
                for statement in _V2_TABLE_DDL.values():
                    self._db().execute(statement)
                self._db().executemany(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    (
                        ("schema_version", "2"),
                        ("migration_marker", "complete"),
                        ("schema_identity", _SCHEMA_IDENTITIES[SchemaVersion(2)]),
                    ),
                )
                self._db().executemany(
                    "INSERT INTO runs(run_id, created_at, closed_at, metadata_json) VALUES (?, ?, NULL, ?)",
                    rows["runs"],
                )
                for table in ("milestones", "dispatches", "events"):
                    placeholders = ", ".join("?" for _ in rows[table][0]) if rows[table] else ""
                    if placeholders:
                        self._db().executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows[table])
                self._fault("after_migration")
        finally:
            self._db().execute("PRAGMA foreign_keys = ON")
        if self._db().execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CorruptSchemaError("migrated ledger violates the canonical foreign-key contract")
        self._migrate_v2_to_v3()

    def _migrate_v18_to_v19(self) -> None:
        """Rename the live supervisor authority in one forward migration.

        The predecessor table and schema identity remain available only to
        migration/provenance readers.  No compatibility view or dual-read
        path is created for the replacement harness.
        """

        self._validate_schema_metadata(SchemaVersion(18))
        self._validate_shape(SchemaVersion(18))
        self._validate_rows()
        harness_exists = (
            self._db()
            .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'harness_authority'")
            .fetchone()
        )
        supervisor_exists = (
            self._db()
            .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'supervisor_authority'")
            .fetchone()
        )
        if harness_exists is not None and supervisor_exists is None:
            # Recovery fixtures may have lowered a v19 marker without
            # restoring the old table name.  The replacement identity is
            # already present, so only advance metadata after validating it.
            self._validate_schema_metadata(SchemaVersion(18))
            self._validate_shape(SchemaVersion(18))
            self._validate_rows()
            with self._transaction(validate_authority=False):
                self._db().execute("UPDATE schema_meta SET value = '19' WHERE key = 'schema_version'")
                self._db().execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                    (_SCHEMA_IDENTITIES[SchemaVersion(19)],),
                )
                self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
                self._fault("after_migration")
            return
        authority = self._db().execute("SELECT * FROM supervisor_authority WHERE singleton = 1").fetchone()
        if authority is not None and int(authority["requested_shutdown"]) not in {0, 1}:
            raise CorruptSchemaError("supervisor authority shutdown fence is malformed")
        with self._transaction(validate_authority=False):
            self._fault("before_rename_supervisor_authority")
            self._db().execute("ALTER TABLE supervisor_authority RENAME TO harness_authority")
            self._fault("after_rename_supervisor_authority")
            self._db().execute("UPDATE schema_meta SET value = '19' WHERE key = 'schema_version'")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(19)],),
            )
            self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
            self._fault("after_migration")

    def _migrate_v12_to_v13(self) -> None:
        """Add live diagnostics, retry policy and control-command authority."""

        self._validate_schema_metadata(SchemaVersion(12))
        self._validate_shape(SchemaVersion(12))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            for table in ("diagnostic_ring", "retry_policies", "control_commands"):
                self._db().execute(_V13_TABLE_DDL[table])
            now = utc_now()
            for row in self._db().execute("SELECT dispatch_id FROM dispatch_queue ORDER BY sequence").fetchall():
                self._db().execute(
                    "INSERT INTO retry_policies(dispatch_id, policy_version, pre_identity_budget, invalid_chain_budget, "
                    "schema_envelope_budget, post_identity_loss_budget, pre_identity_used, invalid_chain_used, "
                    "schema_envelope_used, post_identity_loss_used, last_failure, strategy, next_eligible_at, "
                    "prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at) "
                    "VALUES (?, 1, 5, 1, 2, 1, 0, 0, 0, 0, NULL, 'none', NULL, NULL, NULL, NULL, ?, ?)",
                    (str(row["dispatch_id"]), now, now),
                )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(13))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(13)],),
            )
            self._fault("after_migration")

        self._migrate_v13_to_v14()

    def _migrate_v13_to_v14(self) -> None:
        """Add server command order and the reasoned recovery-control audit."""

        self._validate_schema_metadata(SchemaVersion(13))
        self._validate_shape(SchemaVersion(13))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute("ALTER TABLE retry_policies RENAME TO retry_policies_v13")
            self._db().execute(_RETRY_POLICIES_V14_DDL)
            self._db().execute(
                "INSERT INTO retry_policies(dispatch_id, revision, policy_version, pre_identity_budget, invalid_chain_budget, "
                "schema_envelope_budget, post_identity_loss_budget, pre_identity_used, invalid_chain_used, "
                "schema_envelope_used, post_identity_loss_used, last_failure, strategy, next_eligible_at, "
                "prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at) "
                "SELECT dispatch_id, 0, policy_version, pre_identity_budget, invalid_chain_budget, schema_envelope_budget, "
                "post_identity_loss_budget, pre_identity_used, invalid_chain_used, schema_envelope_used, "
                "post_identity_loss_used, last_failure, strategy, next_eligible_at, prior_thread_id, prior_turn_id, "
                "human_attention_reason, created_at, updated_at FROM retry_policies_v13 ORDER BY rowid"
            )
            self._db().execute("DROP TABLE retry_policies_v13")
            self._db().execute("ALTER TABLE control_commands RENAME TO control_commands_v13")
            self._db().execute(_CONTROL_COMMANDS_V14_DDL)
            self._db().execute(
                "INSERT INTO control_commands(command_id, dispatch_id, submission_sequence, generation, attempt, "
                "thread_id, turn_id, kind, payload, payload_sha256, state, acknowledgement_json, created_at, sent_at, acknowledged_at) "
                "SELECT command_id, dispatch_id, ROW_NUMBER() OVER (PARTITION BY dispatch_id ORDER BY rowid), generation, attempt, "
                "thread_id, turn_id, kind, payload, payload_sha256, state, acknowledgement_json, created_at, sent_at, acknowledged_at "
                "FROM control_commands_v13 ORDER BY dispatch_id, rowid"
            )
            self._db().execute("DROP TABLE control_commands_v13")
            self._db().execute(_RECOVERY_CONTROLS_DDL)
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(14))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(14)],),
            )
            self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
            self._fault("after_migration")
        self._migrate_v14_to_v15()

    def _migrate_v2_to_v3(self) -> None:
        self._validate_schema_metadata(SchemaVersion(2))
        self._validate_shape(SchemaVersion(2))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            for table in ("workspace_leases", "executions", "sdk_lifecycle_events"):
                self._db().execute(_V3_TABLE_DDL[table])
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(3))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(3)],),
            )
            self._fault("after_migration")
        self._migrate_v3_to_v4()

    def _migrate_v3_to_v4(self) -> None:
        self._validate_schema_metadata(SchemaVersion(3))
        self._validate_shape(SchemaVersion(3))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute(_EXECUTION_INTEGRITY_V4_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, sandbox_policy_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT run_id, milestone_id, 'legacy_v3', NULL, NULL, NULL, created_at, updated_at "
                "FROM executions WHERE status != ?",
                (ExecutionStatus.PLANNED.value,),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(4))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(4)],),
            )
            self._fault("after_migration")
        self._migrate_v4_to_v5()

    def _migrate_v4_to_v5(self) -> None:
        self._validate_schema_metadata(SchemaVersion(4))
        self._validate_shape(SchemaVersion(4))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute("ALTER TABLE execution_integrity RENAME TO execution_integrity_v4")
            self._db().execute(_EXECUTION_INTEGRITY_V5_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT run_id, milestone_id, CASE WHEN provenance = 'controller_v1' "
                "THEN 'legacy_sandbox_v4' ELSE 'legacy_v3' END, NULL, git_authority_before_sha256, "
                "git_authority_after_sha256, created_at, updated_at FROM execution_integrity_v4"
            )
            self._db().execute("DROP TABLE execution_integrity_v4")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(5))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(5)],),
            )
            self._fault("after_migration")
        self._migrate_v5_to_v6()

    def _migrate_v5_to_v6(self) -> None:
        self._validate_schema_metadata(SchemaVersion(5))
        self._validate_shape(SchemaVersion(5))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute("ALTER TABLE execution_integrity RENAME TO execution_integrity_v5")
            self._db().execute(_EXECUTION_INTEGRITY_V6_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
                "native_compatibility_sha256, effective_permission_json, effective_permission_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT run_id, milestone_id, CASE WHEN provenance = 'controller_v2' "
                "THEN 'legacy_profile_v5' ELSE provenance END, native_profile_sha256, NULL, NULL, NULL, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at "
                "FROM execution_integrity_v5"
            )
            self._db().execute("DROP TABLE execution_integrity_v5")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(6))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(6)],),
            )
            self._fault("after_migration")
        self._migrate_v6_to_v7()

    def _migrate_v6_to_v7(self) -> None:
        self._validate_schema_metadata(SchemaVersion(6))
        self._validate_shape(SchemaVersion(6))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute("ALTER TABLE execution_integrity RENAME TO execution_integrity_v6")
            self._db().execute(_EXECUTION_INTEGRITY_V7_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
                "native_compatibility_sha256, effective_permission_json, effective_permission_sha256, "
                "workspace_baseline_head_sha, workspace_baseline_json, workspace_baseline_sha256, "
                "turn_started_at, git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT i.run_id, i.milestone_id, CASE WHEN i.provenance = 'controller_v3' "
                "THEN 'legacy_permission_v6' ELSE i.provenance END, i.native_profile_sha256, "
                "i.native_compatibility_sha256, i.effective_permission_json, i.effective_permission_sha256, "
                "NULL, NULL, NULL, CASE WHEN x.turn_id IS NOT NULL OR x.turn_output_json = "
                '\'{"__controller_checkpoint":"turn_starting"}\' THEN x.updated_at ELSE NULL END, '
                "i.git_authority_before_sha256, i.git_authority_after_sha256, i.created_at, i.updated_at "
                "FROM execution_integrity_v6 i JOIN executions x "
                "ON x.run_id = i.run_id AND x.milestone_id = i.milestone_id"
            )
            self._db().execute("DROP TABLE execution_integrity_v6")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(7))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(7)],),
            )
            self._fault("after_migration")
        self._migrate_v7_to_v8()

    def _migrate_v7_to_v8(self) -> None:
        # A pre-H6-E ledger may have been interrupted after metadata was
        # rewound but before the v10 queue tables were cleaned up.  Empty v10
        # tables carry no authority; remove them transactionally so the
        # predecessor shape can still be validated.  Non-empty leftovers are
        # ambiguous and remain fail-closed.
        legacy_leftovers = (
            "notification_outbox",
            "successor_outbox",
            "attempt_capabilities",
            "dispatch_queue",
            "supervisor_authority",
            "recovery_state",
            # A v13 opener may encounter a deliberately rewound v7 marker
            # from a predecessor migration fixture.  Live-control tables are
            # owned by that newer marker and must be removed with the other
            # empty residue before the exact v7 shape can be validated.
            "diagnostic_ring",
            "retry_policies",
            "control_commands",
            "recovery_controls",
        )
        present = [
            table
            for table in legacy_leftovers
            if self._db().execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        ]
        if present:
            # Recovery rows cannot be meaningful once their queue authority
            # has been removed by an interrupted predecessor migration.
            if "recovery_state" in present and "dispatch_queue" not in present:
                with self._transaction(validate_authority=False):
                    self._db().execute("DROP TABLE recovery_state")
                present.remove("recovery_state")
            if any(int(self._db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in present):
                raise CorruptSchemaError("non-empty H6-E tables conflict with a legacy schema marker")
            with self._transaction(validate_authority=False):
                for table in legacy_leftovers:
                    if table in present:
                        self._db().execute(f"DROP TABLE {table}")
        self._validate_schema_metadata(SchemaVersion(7))
        self._validate_shape(SchemaVersion(7), allow_program_residue=True)
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute("ALTER TABLE execution_integrity RENAME TO execution_integrity_v7")
            self._db().execute(_EXECUTION_INTEGRITY_DDL)
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
                "native_compatibility_sha256, effective_permission_json, effective_permission_sha256, "
                "workspace_baseline_head_sha, workspace_baseline_json, workspace_baseline_sha256, "
                "workspace_terminal_head_sha, workspace_terminal_json, workspace_terminal_sha256, "
                "turn_started_at, git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "SELECT run_id, milestone_id, provenance, native_profile_sha256, native_compatibility_sha256, "
                "effective_permission_json, effective_permission_sha256, workspace_baseline_head_sha, "
                "workspace_baseline_json, workspace_baseline_sha256, NULL, NULL, NULL, turn_started_at, "
                "git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at "
                "FROM execution_integrity_v7"
            )
            self._db().execute("DROP TABLE execution_integrity_v7")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(8))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(8)],),
            )
            self._fault("after_migration")
        self._migrate_v8_to_v9()

    def _migrate_v8_to_v9(self) -> None:
        self._validate_schema_metadata(SchemaVersion(8))
        self._validate_shape(SchemaVersion(8))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._db().execute(_APP_NATIVE_DISPATCHES_DDL)
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(9))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(9)],),
            )
            self._fault("after_migration")
        self._migrate_v9_to_v10()

    def _migrate_v9_to_v10(self) -> None:
        """Add the H6-E queue in one serialized, crash-atomic transaction."""

        self._validate_schema_metadata(SchemaVersion(9))
        draft = False
        app_row = (
            self._db()
            .execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'app_native_dispatches'")
            .fetchone()
        )
        if app_row is not None:
            draft = _canonical_ddl(str(app_row[0])) == _canonical_ddl(_APP_NATIVE_DISPATCHES_DRAFT_V9_DDL)
        if draft:
            if int(self._db().execute("SELECT COUNT(*) FROM app_native_dispatches").fetchone()[0]) != 0:
                raise UnsupportedSchemaVersion("non-empty draft schema v9 requires explicit recovery before upgrade")
        else:
            self._validate_shape(SchemaVersion(9))
            self._validate_rows()
        with self._transaction(validate_authority=False):
            if draft:
                self._db().execute("DROP TABLE app_native_dispatches")
                self._db().execute(_APP_NATIVE_DISPATCHES_DDL)
            for table in (
                "supervisor_authority",
                "dispatch_queue",
                "attempt_capabilities",
                "successor_outbox",
                "notification_outbox",
            ):
                self._db().execute(_V10_TABLE_DDL[table])
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(10))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(10)],),
            )
            self._fault("after_migration")
        # A v9 opener must reach the current schema in the same serialized
        # migration chain.  Returning at v10 leaves _ensure_schema validating
        # a v10 marker as v11 and turns otherwise valid legacy ledgers into a
        # false corruption report.
        self._migrate_v10_to_v11()

    def _migrate_v10_to_v11(self) -> None:
        """Add H6-E-W bindings and wake authority in one transaction."""

        self._validate_schema_metadata(SchemaVersion(10))
        authority = self._db().execute("SELECT * FROM supervisor_authority WHERE singleton = 1").fetchone()
        if authority is not None and str(authority["expires_at"]) > utc_now():
            pid = int(authority["pid"])
            try:
                os.kill(pid, 0)
                live = _process_birth_identity(pid) == str(authority["process_birth_identity"])
            except OSError:
                live = False
            if live:
                raise UnsupportedSchemaVersion(
                    "live schema-v10 supervisor owns the ledger; stop or upgrade it before migration"
                )
        # A v12 opener may encounter a deliberately downgraded v10 marker
        # (used for live-supervisor recovery tests) while the empty v12 table
        # remains.  Empty residue carries no authority and is removed before
        # validating the predecessor shape; non-empty residue is ambiguous.
        recovery_table = (
            self._db()
            .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_state'")
            .fetchone()
        )
        if recovery_table is not None:
            if int(self._db().execute("SELECT COUNT(*) FROM recovery_state").fetchone()[0]) != 0:
                raise UnsupportedSchemaVersion("non-empty v12 recovery state conflicts with a v10 marker")
            with self._transaction(validate_authority=False):
                self._db().execute("DROP TABLE recovery_state")
        self._validate_shape(SchemaVersion(10))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            # A v10 supervisor validates the schema marker on every write. A
            # concurrent v11 opener must not migrate underneath a still-live
            # owner and thereby remove the only observer of its worker.
            authority = self._db().execute("SELECT * FROM supervisor_authority WHERE singleton = 1").fetchone()
            if authority is not None and str(authority["expires_at"]) > utc_now():
                pid = int(authority["pid"])
                try:
                    os.kill(pid, 0)
                    live = _process_birth_identity(pid) == str(authority["process_birth_identity"])
                except OSError:
                    live = False
                if live:
                    raise UnsupportedSchemaVersion(
                        "live schema-v10 supervisor owns the ledger; stop or upgrade it before migration"
                    )
            for table in ("queue_bindings", "worker_liveness", "authorized_successors", "wake_outbox"):
                self._db().execute(_V11_TABLE_DDL[table])
            now = utc_now()
            legacy_permission = _encode_json(
                {
                    "mode": NativePermissionMode.READ_ONLY.value,
                    "sandbox_mode": "read-only",
                    "approval_policy": "never",
                    "monotonic": True,
                }
            )
            legacy_permission_sha256 = hashlib.sha256(legacy_permission.encode("utf-8")).hexdigest()
            for row in self._db().execute("SELECT * FROM dispatch_queue ORDER BY sequence").fetchall():
                projection_sha256 = hashlib.sha256(str(row["capsule_json"]).encode("utf-8")).hexdigest()
                self._db().execute(
                    "INSERT INTO queue_bindings(dispatch_id, plan_path, plan_revision_sha256, projection_sha256, "
                    "source_thread_id, permission_mode, native_profile_sha256, native_compatibility_sha256, "
                    "effective_permission_json, effective_permission_sha256, checkpoint_deadline, checkpoint_armed, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, NULL, 0, ?, ?)",
                    (
                        row["dispatch_id"],
                        "legacy-v10",
                        projection_sha256,
                        projection_sha256,
                        NativePermissionMode.READ_ONLY.value,
                        legacy_permission,
                        legacy_permission_sha256,
                        now,
                        now,
                    ),
                )
                if row["state"] in {"completed", "failed"}:
                    payload = _encode_json(
                        {
                            "schema_version": 1,
                            "kind": "TERMINAL",
                            "dispatch_id": row["dispatch_id"],
                            "state": row["state"],
                            "result_sha256": row["raw_result_sha256"],
                            "ledger_path": os.fspath(self.path),
                        }
                    )
                    self._db().execute(
                        "INSERT INTO wake_outbox(delivery_id, dispatch_id, kind, source_thread_id, payload_json, "
                        "payload_digest, state, attempt_count, source_turn_id, created_at, updated_at) "
                        "VALUES (?, ?, 'terminal', NULL, ?, ?, 'not_applicable', 0, NULL, ?, ?)",
                        (
                            f"wake/{row['dispatch_id']}/terminal",
                            row["dispatch_id"],
                            payload,
                            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                            now,
                            now,
                        ),
                    )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(11))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(11)],),
            )
            self._fault("after_migration")

        self._migrate_v11_to_v12()

    def _migrate_v11_to_v12(self) -> None:
        """Add crash-atomic worker-exit and bounded recovery authority."""

        self._validate_schema_metadata(SchemaVersion(11))
        # A v11 marker together with a recovery table is a mixed-schema
        # residue, not a resumable migration.  Fail closed rather than
        # accepting an object whose marker and inventory disagree.
        recovery_residue = (
            self._db()
            .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_state'")
            .fetchone()
        )
        if recovery_residue is not None:
            raise CorruptSchemaError("schema-v11 ledger contains unexpected v12 recovery state")
        self._validate_shape(SchemaVersion(11))
        # The donor v11 ledger can contain the documented stale-worker shape:
        # its process-exit fact is durable, while the queue projection still
        # says starting/running.  Permit that one predecessor inconsistency so
        # this migration can repair it atomically; all other row invariants
        # remain fail-closed.
        self._validate_rows(allow_exited_active=True)
        with self._transaction(validate_authority=False):
            # The predecessor queue CHECK constraint does not know the v12
            # recovery states.  Rebuild only that parent and its direct child
            # tables in the same transaction, preserving every row and FK
            # while making the new enum writable.  SQLite has no ALTER CHECK
            # operation; leaving the old table in place would make the stale
            # worker fixture impossible to repair atomically.
            legacy_queue = "dispatch_queue_v11"
            self._db().execute(f"ALTER TABLE dispatch_queue RENAME TO {legacy_queue}")
            self._db().execute(_DISPATCH_QUEUE_DDL)
            self._db().execute(f"INSERT INTO dispatch_queue SELECT * FROM {legacy_queue}")
            direct_children = (
                "attempt_capabilities",
                "successor_outbox",
                "notification_outbox",
                "queue_bindings",
                "worker_liveness",
                "authorized_successors",
                "wake_outbox",
            )
            for table in direct_children:
                legacy_child = f"{table}_v11"
                self._db().execute(f"ALTER TABLE {table} RENAME TO {legacy_child}")
                self._db().execute(_V11_TABLE_DDL[table])
                self._db().execute(f"INSERT INTO {table} SELECT * FROM {legacy_child}")
                self._db().execute(f"DROP TABLE {legacy_child}")
            self._db().execute(f"DROP TABLE {legacy_queue}")
            self._db().execute(_RECOVERY_STATE_DDL)
            now = utc_now()
            for row in (
                self._db()
                .execute(
                    "SELECT q.dispatch_id, q.state, q.route_json, l.exited_at AS worker_exited_at "
                    "FROM dispatch_queue q LEFT JOIN worker_liveness l ON l.dispatch_id = q.dispatch_id "
                    "ORDER BY q.sequence"
                )
                .fetchall()
            ):
                provider_env_key = None
                profile_sha256 = None
                try:
                    route = json.loads(str(row["route_json"]))
                    if isinstance(route, dict):
                        candidate_key = route.get("provider_env_key")
                        candidate_profile = route.get("profile_sha256") or route.get("native_profile_sha256")
                        if isinstance(candidate_key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", candidate_key):
                            provider_env_key = candidate_key
                        if isinstance(candidate_profile, str) and re.fullmatch(r"[0-9a-f]{64}", candidate_profile):
                            profile_sha256 = candidate_profile
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                stale_exit = row["worker_exited_at"] is not None and row["state"] in {
                    "claimed",
                    "starting",
                    "running",
                }
                migrated_state = "recovery_inspection_pending" if stale_exit else "none"
                migrated_used = 1 if stale_exit else 0
                self._db().execute(
                    "INSERT INTO recovery_state(dispatch_id, provider_env_key, profile_sha256, "
                    "worker_exit_classification, exit_code, recovery_state, inspection_budget, inspection_used, "
                    "continuation_budget, continuation_used, fresh_thread_after_idle, fresh_thread_budget, "
                    "fresh_thread_used, next_eligible_at, inspected_thread_id, inspected_turn_id, inspected_kind, "
                    "human_attention_reason, exited_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, 4, ?, 1, 0, 0, 0, 0, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (
                        str(row["dispatch_id"]),
                        provider_env_key,
                        profile_sha256,
                        "migrated-worker-exit" if stale_exit else None,
                        migrated_state,
                        migrated_used,
                        None,
                        row["worker_exited_at"] if stale_exit else None,
                        now,
                        now,
                    ),
                )
                if stale_exit:
                    self._db().execute(
                        "UPDATE dispatch_queue SET state = 'recovery_inspection_pending', updated_at = ? "
                        "WHERE dispatch_id = ? AND state IN ('claimed','starting','running')",
                        (now, str(row["dispatch_id"])),
                    )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (str(int(SchemaVersion(12))),),
            )
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(12)],),
            )
            self._fault("after_migration")

        self._migrate_v12_to_v13()

    def _migrate_v14_to_v15(self) -> None:
        """Upgrade exact v14 authority to decision-bound v15 atomically."""

        self._validate_schema_metadata(SchemaVersion(14))
        self._validate_shape(SchemaVersion(14))
        self._validate_rows()
        old_wakes = self._db().execute("SELECT * FROM wake_outbox ORDER BY delivery_id").fetchall()
        queue_rows = {
            str(row["dispatch_id"]): row
            for row in self._db().execute("SELECT * FROM dispatch_queue ORDER BY sequence").fetchall()
        }
        with self._transaction(validate_authority=False):
            self._fault("before_create_controller_decisions")
            self._db().execute(_CONTROLLER_DECISIONS_DDL)
            self._fault("after_create_controller_decisions")
            self._fault("before_create_controller_decision_generations")
            self._db().execute(_CONTROLLER_GENERATIONS_DDL)
            self._fault("after_create_controller_decision_generations")
            self._fault("before_create_controller_action_outbox")
            self._db().execute(_CONTROLLER_ACTION_OUTBOX_DDL)
            self._fault("after_create_controller_action_outbox")
            for name, (_table, statement) in _V15_INDEX_DDL.items():
                self._fault(f"before_create_{name}")
                self._db().execute(statement)
                self._fault(f"after_create_{name}")
            self._fault("after_create_controller_indexes")

            self._fault("before_rename_wake_outbox")
            self._db().execute("ALTER TABLE wake_outbox RENAME TO wake_outbox_v14")
            self._fault("after_rename_wake_outbox")
            self._fault("before_create_wake_outbox")
            self._db().execute(_WAKE_OUTBOX_V15_DDL)
            self._fault("after_create_wake_outbox")
            now = utc_now()
            for wake in old_wakes:
                delivery_id = str(wake["delivery_id"])
                decision_id = f"decision/{delivery_id}"
                raw_payload = str(wake["payload_json"])
                try:
                    payload = strict_json_loads(raw_payload, max_bytes=16_384)
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                dispatch_id = str(wake["dispatch_id"])
                queue = queue_rows.get(dispatch_id)
                queue_state = str(queue["state"]) if queue is not None else ""
                kind = str(wake["kind"])
                wake_state = str(wake["state"])
                if queue_state in {"completed", "failed", "cancelled"} and wake_state in {
                    "delivered",
                    "not_applicable",
                }:
                    decision_state = ControllerDecisionState.LEGACY_CLOSED.value
                elif wake_state == "pending":
                    decision_state = ControllerDecisionState.PENDING_DELIVERY.value
                elif wake_state in {"starting", "ambiguous", "failed"}:
                    decision_state = ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                else:
                    decision_state = ControllerDecisionState.AWAITING_CLAIM.value
                source_thread_id = wake["source_thread_id"]
                summary = {
                    "dispatch_id": dispatch_id,
                    "kind": kind,
                    "state": queue_state,
                    "source_thread_id": source_thread_id,
                    "result_sha256": payload.get("result_sha256")
                    if isinstance(payload.get("result_sha256"), str)
                    else None,
                }
                summary_json = _encode_json(summary)
                summary_sha = hashlib.sha256(summary_json.encode("utf-8")).hexdigest()
                human_reason = (
                    "legacy wake requires human attention"
                    if decision_state == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                    else None
                )
                if decision_state == ControllerDecisionState.LEGACY_CLOSED.value:
                    generation_state = ControllerGenerationState.SUPERSEDED.value
                else:
                    generation_state = ControllerGenerationState.PREPARED.value
                self._fault("before_copy_wake_row")
                self._db().execute(
                    "INSERT INTO controller_decisions(decision_id, dispatch_id, kind, cycle_sequence, summary_json, summary_sha256, source_thread_id, state, revision, current_generation, generation_budget, generation_used, claimant_kind, claimant_id, claim_token_sha256, claim_started_at, claim_lease_expires_at, action_id, action_bundle_json, action_bundle_sha256, committed_at, acknowledged_at, superseded_at, deadline, human_attention_reason, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, 1, 2, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (
                        decision_id,
                        dispatch_id,
                        kind,
                        summary_json,
                        summary_sha,
                        source_thread_id,
                        decision_state,
                        str(wake["updated_at"]),
                        human_reason,
                        str(wake["created_at"]),
                        now,
                    ),
                )
                self._db().execute(
                    "INSERT INTO controller_decision_generations(decision_id, generation, lineage_id, predecessor_generation, source_kind, state, prompt_sha256, controller_thread_id, controller_turn_id, inspection_started_at, inspection_completed_at, inspection_outcome, started_at, terminal_at, created_at, updated_at) VALUES (?, 1, ?, NULL, 'source_controller', ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                    (decision_id, f"lineage/{decision_id}", generation_state, str(wake["created_at"]), now),
                )
                self._db().execute(
                    "INSERT INTO wake_outbox(delivery_id, dispatch_id, decision_id, kind, cycle_sequence, source_thread_id, payload_json, payload_digest, state, attempt_count, source_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        wake["delivery_id"],
                        wake["dispatch_id"],
                        decision_id,
                        wake["kind"],
                        wake["source_thread_id"],
                        wake["payload_json"],
                        wake["payload_digest"],
                        wake["state"],
                        wake["attempt_count"],
                        wake["source_turn_id"],
                        wake["created_at"],
                        wake["updated_at"],
                    ),
                )
                self._fault("after_copy_wake_row")
            self._fault("before_drop_wake_outbox")
            self._db().execute("DROP TABLE wake_outbox_v14")
            self._fault("after_drop_wake_outbox")
            self._fault("before_schema_version_update")
            self._db().execute("UPDATE schema_meta SET value = '15' WHERE key = 'schema_version'")
            self._fault("after_schema_version_update")
            self._fault("before_schema_identity_update")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(15)],),
            )
            self._fault("after_schema_identity_update")
            self._fault("before_migration_marker_update")
            self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
            self._fault("after_migration")
        self._migrate_v15_to_v16()

    def _migrate_v15_to_v16(self) -> None:
        """Extend the existing recovery-action authority for exact runtime rebinds."""

        self._validate_schema_metadata(SchemaVersion(15))
        self._validate_shape(SchemaVersion(15))
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._fault("before_rename_recovery_controls")
            self._db().execute("ALTER TABLE recovery_controls RENAME TO recovery_controls_v15")
            self._fault("after_rename_recovery_controls")
            self._db().execute(_RECOVERY_CONTROLS_V16_DDL)
            self._fault("after_create_recovery_controls")
            self._db().execute(
                "INSERT INTO recovery_controls(action_id, dispatch_id, expected_revision, action_kind, reason, "
                "requested_budget_json, compatibility_rebind_json, applied_revision, created_at) "
                "SELECT action_id, dispatch_id, expected_revision, action_kind, reason, requested_budget_json, "
                "NULL, applied_revision, created_at FROM recovery_controls_v15"
            )
            self._fault("after_copy_recovery_controls")
            self._db().execute("DROP TABLE recovery_controls_v15")
            self._fault("after_drop_recovery_controls")
            self._db().execute("UPDATE schema_meta SET value = '16' WHERE key = 'schema_version'")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(16)],),
            )
            self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
            self._fault("after_migration")
        self._migrate_v16_to_v17()

    def _migrate_v16_to_v17(self) -> None:
        """Add durable provider-transient retry facts to the existing policy table."""

        self._validate_schema_metadata(SchemaVersion(16))
        self._validate_shape(SchemaVersion(16), allow_program_residue=self._has_program_schema_residue())
        self._validate_rows()
        with self._transaction(validate_authority=False):
            self._fault("before_rename_retry_policies")
            self._db().execute("ALTER TABLE retry_policies RENAME TO retry_policies_v16")
            self._fault("after_rename_retry_policies")
            self._db().execute(_RETRY_POLICIES_V17_DDL)
            self._fault("after_create_retry_policies")
            self._db().execute(
                "INSERT INTO retry_policies(dispatch_id, revision, policy_version, pre_identity_budget, "
                "invalid_chain_budget, schema_envelope_budget, post_identity_loss_budget, pre_identity_used, "
                "invalid_chain_used, schema_envelope_used, post_identity_loss_used, last_failure, strategy, "
                "next_eligible_at, prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at, "
                "provider_transient_budget, provider_transient_used, provider_transient_grant_used) "
                "SELECT dispatch_id, revision, policy_version, pre_identity_budget, invalid_chain_budget, "
                "schema_envelope_budget, post_identity_loss_budget, pre_identity_used, invalid_chain_used, "
                "schema_envelope_used, post_identity_loss_used, last_failure, strategy, next_eligible_at, "
                "prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at, 3, 0, 0 "
                "FROM retry_policies_v16 ORDER BY rowid"
            )
            self._fault("after_copy_retry_policies")
            self._db().execute("DROP TABLE retry_policies_v16")
            self._fault("after_drop_retry_policies")
            self._db().execute("UPDATE schema_meta SET value = '17' WHERE key = 'schema_version'")
            self._db().execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                (_SCHEMA_IDENTITIES[SchemaVersion(17)],),
            )
            self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
            self._fault("after_migration")
        self._migrate_v17_to_v18()

    def _migrate_v17_to_v18(self) -> None:
        """Add program identity, dependency edges and integration receipts."""

        self._validate_schema_metadata(SchemaVersion(17))
        if self._has_complete_program_schema():
            try:
                self._db().execute("PRAGMA foreign_keys = OFF")
                with self._transaction(validate_authority=False):
                    self._migrate_v17_dispatches_to_v18()
                    self._db().execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
                    self._db().execute(
                        "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                        (_SCHEMA_IDENTITIES[SchemaVersion(18)],),
                    )
                    self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
                    self._fault("after_migration")
            finally:
                self._db().execute("PRAGMA foreign_keys = ON")
            self._migrate_v18_to_v19()
            return
        self._validate_shape(SchemaVersion(17), allow_program_residue=self._has_program_schema_residue())
        self._validate_rows()
        legacy_decision_columns = (
            "decision_id, dispatch_id, kind, cycle_sequence, summary_json, summary_sha256, source_thread_id, state, "
            "revision, current_generation, generation_budget, generation_used, claimant_kind, claimant_id, "
            "claim_token_sha256, claim_started_at, claim_lease_expires_at, action_id, action_bundle_json, "
            "action_bundle_sha256, committed_at, acknowledged_at, superseded_at, deadline, human_attention_reason, "
            "created_at, updated_at"
        )
        try:
            self._db().execute("PRAGMA foreign_keys = OFF")
            with self._transaction(validate_authority=False):
                self._migrate_v17_dispatches_to_v18()
                for table in ("integration_outbox", "milestone_dependencies"):
                    residue = (
                        self._db()
                        .execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
                        .fetchone()
                    )
                    if residue is not None:
                        if int(self._db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) != 0:
                            raise CorruptSchemaError("non-empty program schema residue conflicts with a legacy marker")
                        self._db().execute(f"DROP TABLE {table}")
                self._fault("before_create_program_runs")
                legacy_run_rows = (
                    self._db().execute("SELECT run_id, created_at, closed_at, metadata_json FROM runs").fetchall()
                )
                self._db().execute("DROP TABLE runs")
                self._db().execute(_PROGRAM_RUNS_DDL)
                self._db().executemany(
                    "INSERT INTO runs(run_id, created_at, closed_at, metadata_json, program_digest, program_revision, "
                    "program_state, program_graph_json, trunk_head) VALUES (?, ?, ?, ?, NULL, 0, NULL, NULL, NULL)",
                    [tuple(row) for row in legacy_run_rows],
                )
                self._fault("after_create_program_runs")
                self._fault("before_create_program_controller_decisions")
                legacy_decisions = self._db().execute("SELECT * FROM controller_decisions").fetchall()
                self._db().execute("DROP TABLE controller_decisions")
                self._db().execute(_PROGRAM_CONTROLLER_DECISIONS_DDL)
                legacy_names = tuple(item.strip() for item in legacy_decision_columns.split(","))
                self._db().executemany(
                    "INSERT INTO controller_decisions(" + legacy_decision_columns + ", program_id, program_revision, "
                    "event_kind, event_key) VALUES (" + ",".join("?" for _ in range(len(legacy_names) + 4)) + ")",
                    [(*tuple(row[name] for name in legacy_names), None, None, None, None) for row in legacy_decisions],
                )
                self._fault("after_create_program_controller_decisions")
                self._db().execute(_MILESTONE_DEPENDENCIES_DDL)
                self._db().execute(_INTEGRATION_OUTBOX_DDL)
                for _name, (_table, statement) in _V15_INDEX_DDL.items():
                    if _table == "controller_decisions":
                        self._db().execute(statement)
                self._fault("after_create_program_tables")
                self._db().execute("UPDATE schema_meta SET value = '18' WHERE key = 'schema_version'")
                self._db().execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_identity'",
                    (_SCHEMA_IDENTITIES[SchemaVersion(18)],),
                )
                self._db().execute("UPDATE schema_meta SET value = 'complete' WHERE key = 'migration_marker'")
                self._fault("after_migration")
        finally:
            self._db().execute("PRAGMA foreign_keys = ON")
        self._migrate_v18_to_v19()

    def _migrate_v17_dispatches_to_v18(self) -> None:
        """Replace legacy role-only uniqueness with generation identity."""

        row = (
            self._db().execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dispatches'").fetchone()
        )
        if row is None or row[0] is None:
            raise CorruptSchemaError("dispatches table is missing during program migration")
        actual = _canonical_ddl(str(row[0]))
        if actual == _canonical_ddl(_PROGRAM_DISPATCHES_DDL):
            return
        if actual != _canonical_ddl(_DISPATCHES_DDL):
            raise CorruptSchemaError("dispatches table has an unsupported pre-program schema")
        self._fault("before_create_program_dispatches")
        self._db().execute(
            _PROGRAM_DISPATCHES_DDL.replace("CREATE TABLE dispatches", "CREATE TABLE dispatches_program_v18", 1)
        )
        self._fault("after_create_program_dispatches")
        self._db().execute(
            "INSERT INTO dispatches_program_v18(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
            "SELECT dispatch_id, run_id, milestone_id, role, generation, claimed_at FROM dispatches ORDER BY rowid"
        )
        self._fault("after_copy_program_dispatches")
        self._db().execute("DROP TABLE dispatches")
        self._db().execute("ALTER TABLE dispatches_program_v18 RENAME TO dispatches")
        self._fault("after_replace_program_dispatches")

    @contextmanager
    def _transaction(self, *, validate_authority: bool = True) -> Iterator[None]:
        connection = self._db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._validate_live_identity()
            if validate_authority:
                self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
                self._validate_shape(CURRENT_SCHEMA_VERSION)
                self._validate_rows()
                self._fault("after_authority_validation")
            yield
            self._fault("before_commit")
            self._validate_live_identity()
            if validate_authority:
                self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
                self._validate_shape(CURRENT_SCHEMA_VERSION)
                self._validate_rows()
            self._commit_transaction(connection)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise

    @contextmanager
    def _diagnostic_transaction(self) -> Iterator[None]:
        """Commit one bounded non-authoritative ring mutation safely.

        Diagnostic rows never authorize lifecycle transitions. Their hot path
        pins the ledger file and exact schema identity at both boundaries while
        SQLite enforces FK/CHECK constraints; the caller additionally validates
        the selected dispatch's complete bounded ring. This deliberately cannot
        be selected by arbitrary ledger mutations.
        """

        connection = self._db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._validate_live_identity()
            self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
            self._validate_diagnostic_schema_identity()
            yield
            self._fault("before_commit")
            self._validate_live_identity()
            self._validate_schema_metadata(CURRENT_SCHEMA_VERSION)
            self._validate_diagnostic_schema_identity()
            self._commit_transaction(connection)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise

    def _validate_diagnostic_schema_identity(self) -> None:
        """Validate the closed tables and constraints used by the fast path."""

        foreign_keys = self._db().execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise CorruptSchemaError("foreign-key enforcement is disabled")
        for table in (
            "harness_authority",
            "dispatch_queue",
            "attempt_capabilities",
            "worker_liveness",
            "diagnostic_ring",
        ):
            actual = (
                self._db()
                .execute("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,))
                .fetchone()
            )
            expected = _V19_TABLE_DDL[table]
            if actual is None or actual[0] is None or _canonical_ddl(str(actual[0])) != _canonical_ddl(expected):
                raise CorruptSchemaError(f"diagnostic transaction table identity changed: {table}")

    def _commit_transaction(self, connection: sqlite3.Connection) -> None:
        """Authorize identity at SQLite's transaction-commit boundary."""

        boundary_error: BaseException | None = None

        def authorize(
            action_code: int,
            operation: str | None,
            _argument: str | None,
            _database: str | None,
            _source: str | None,
        ) -> int:
            nonlocal boundary_error
            if action_code == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                try:
                    self._fault("at_commit_boundary")
                    self._validate_live_identity()
                except BaseException as exc:
                    boundary_error = exc
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            if boundary_error is not None:
                raise boundary_error from exc
            raise
        finally:
            connection.set_authorizer(None)

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def close(self) -> None:
        self._close_connection()
        if self._database_fd is not None:
            os.close(self._database_fd)
            self._database_fd = None
        for descriptor in reversed(self._directory_fds):
            os.close(descriptor)
        self._directory_fds = ()
        self._open_identity = None

    def reopen(self) -> Ledger:
        self.close()
        self._open()
        return self

    def open(self) -> Ledger:
        self._open()
        return self

    def __enter__(self) -> Ledger:
        self._open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _verify_run(self, expected: RunRecord) -> RunRecord:
        row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(expected.run_id),)).fetchone()
        if row is None:
            raise CorruptSchemaError("successful run write did not leave its durable row")
        actual = self._run_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful run write was rewritten before commit")
        return actual

    def _verify_milestone(self, expected: MilestoneRecord) -> MilestoneRecord:
        row = (
            self._db()
            .execute(
                "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                (str(expected.run_id), str(expected.milestone_id)),
            )
            .fetchone()
        )
        if row is None:
            raise CorruptSchemaError("successful milestone write did not leave its durable row")
        actual = self._milestone_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful milestone write was rewritten before commit")
        return actual

    def _verify_dispatch(self, expected: DispatchClaim) -> DispatchClaim:
        row = (
            self._db()
            .execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(expected.dispatch_id),))
            .fetchone()
        )
        if row is None:
            raise CorruptSchemaError("successful dispatch claim did not leave its durable row")
        actual = self._dispatch_from_row(row)
        if actual != expected:
            raise CorruptSchemaError("successful dispatch claim was rewritten before commit")
        return actual

    def _verify_event(self, expected: EventRecord) -> EventRecord:
        row = self._db().execute("SELECT * FROM events WHERE event_id = ?", (str(expected.event_id),)).fetchone()
        if row is None:
            raise CorruptSchemaError("successful event write did not leave its durable row")
        actual = self._event_from_row(row)
        actual_reason = (actual.reason.code, actual.reason.detail) if actual.reason is not None else None
        expected_reason = (expected.reason.code, expected.reason.detail) if expected.reason is not None else None
        if (
            actual.event_id,
            actual.run_id,
            actual.milestone_id,
            actual.sequence,
            actual.from_state,
            actual.to_state,
            actual.event_type,
            actual_reason,
            actual.occurred_at,
            actual.dispatch_id,
            actual.data,
        ) != (
            expected.event_id,
            expected.run_id,
            expected.milestone_id,
            expected.sequence,
            expected.from_state,
            expected.to_state,
            expected.event_type,
            expected_reason,
            expected.occurred_at,
            expected.dispatch_id,
            expected.data,
        ):
            raise CorruptSchemaError("successful event write was rewritten before commit")
        return actual

    # ------------------------------------------------------------------
    # Record creation and lookup
    # ------------------------------------------------------------------

    def create_run(self, run_id: RunId | str, *, metadata: Mapping[str, object] | None = None) -> RunRecord:
        run = _run_id(run_id)
        checked, encoded = _empty_object(metadata, field_name="run metadata")
        created = utc_now()
        with self._transaction():
            existing = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="run metadata") != checked:
                    raise LedgerError(f"run {run} already exists with different metadata")
                return self._verify_run(self._run_from_row(existing))
            expected = RunRecord(run, created, None, checked)
            self._db().execute(
                "INSERT INTO runs(run_id, created_at, closed_at, metadata_json) VALUES (?, ?, NULL, ?)",
                (str(run), created, encoded),
            )
            return self._verify_run(expected)

    def get_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(run),)).fetchone()
        if row is None:
            raise RecordNotFound(f"run {run} does not exist")
        return self._run_from_row(row)

    def close_run(self, run_id: RunId | str) -> RunRecord:
        run = _run_id(run_id)
        closed = utc_now()
        with self._transaction():
            existing = self.get_run(run)
            expected = RunRecord(existing.run_id, existing.created_at, existing.closed_at or closed, existing.metadata)
            self._db().execute(
                "UPDATE runs SET closed_at = COALESCE(closed_at, ?) WHERE run_id = ?", (closed, str(run))
            )
            return self._verify_run(expected)

    def create_milestone(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> MilestoneRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        checked, encoded = _empty_object(metadata, field_name="milestone metadata")
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            existing = (
                self._db()
                .execute("SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
                .fetchone()
            )
            if existing is not None:
                if _decode_object(existing["metadata_json"], field_name="milestone metadata") != checked:
                    raise LedgerError(f"milestone {milestone} already exists with different metadata")
                return self._verify_milestone(self._milestone_from_row(existing))
            expected = MilestoneRecord(run, milestone, WorkflowState.PLANNED, now, now, checked)
            self._db().execute(
                "INSERT INTO milestones(run_id, milestone_id, current_state, created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(run), str(milestone), WorkflowState.PLANNED.value, now, now, encoded),
            )
            return self._verify_milestone(expected)

    def get_milestone(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> MilestoneRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute("SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"milestone {milestone} does not exist")
        return self._milestone_from_row(row)

    def current_state(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> WorkflowState:
        return self.get_milestone(run_id, milestone_id).state

    # ------------------------------------------------------------------
    # Dispatch claims and state transitions
    # ------------------------------------------------------------------

    def next_program_dispatch_generation(
        self, run_id: RunId | str, milestone_id: MilestoneId | str, role: RoleId | str
    ) -> int:
        """Return the next historical generation for one program role."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        role_value = _role(role)
        row = (
            self._db()
            .execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation FROM dispatches "
                "WHERE run_id = ? AND milestone_id = ? AND role = ?",
                (str(run), str(milestone), str(role_value)),
            )
            .fetchone()
        )
        return int(row["generation"]) + 1 if row is not None else 1

    def _require_next_dispatch_generation(
        self, run: RunId, milestone: MilestoneId, role: RoleId, generation: int
    ) -> None:
        row = (
            self._db()
            .execute(
                "SELECT COALESCE(MAX(generation), 0) AS generation FROM dispatches "
                "WHERE run_id = ? AND milestone_id = ? AND role = ?",
                (str(run), str(milestone), str(role)),
            )
            .fetchone()
        )
        latest = int(row["generation"]) if row is not None else 0
        if generation != latest + 1:
            raise DispatchConflict(f"milestone {milestone} role {role} requires successor generation {latest + 1}")

    def claim_dispatch(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        role: RoleId | str,
        generation: Generation | int | str,
    ) -> DispatchClaim:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        role_value = _role(role)
        generation_value = _generation(generation)
        if generation_value != Generation(1):
            raise DispatchConflict("initial dispatch claim requires generation 1")
        dispatch_id = DispatchId.from_parts(run, milestone, role_value, generation_value)
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            existing = (
                self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if existing is not None:
                return self._verify_dispatch(self._dispatch_from_row(existing))
            owner = (
                self._db()
                .execute(
                    "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = ?",
                    (str(run), str(milestone), str(role_value)),
                )
                .fetchone()
            )
            if owner is not None:
                raise DispatchConflict(f"milestone {milestone} role {role_value} is owned by {owner['dispatch_id']}")
            current = self.get_milestone(run, milestone)
            if current.state is not WorkflowState.PLANNED:
                raise InvalidTransition(f"dispatch claim requires PLANNED, found {current.state.value}")
            self._db().execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(dispatch_id), str(run), str(milestone), str(role_value), int(generation_value), now),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.STARTING.value, now, str(run), str(milestone)),
            )
            self._fault("after_state_update")
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.PLANNED,
                to_state=WorkflowState.STARTING,
                event_type="dispatch_claimed",
                reason=WorkflowReason(ReasonCode.DISPATCH_CLAIMED),
                dispatch_id=dispatch_id,
                data=None,
            )
            self._fault("after_event_insert")
            expected_claim = DispatchClaim(dispatch_id, run, milestone, role_value, generation_value, now)
            expected_milestone = MilestoneRecord(
                current.run_id,
                current.milestone_id,
                WorkflowState.STARTING,
                current.created_at,
                now,
                current.metadata,
            )
            self._verify_dispatch(expected_claim)
            self._verify_milestone(expected_milestone)
            self._verify_event(event)
            return expected_claim

    def get_dispatch(self, dispatch_id: DispatchId | str) -> DispatchClaim:
        dispatch = DispatchId(dispatch_id)
        row = self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch),)).fetchone()
        if row is None:
            raise RecordNotFound(f"dispatch {dispatch} does not exist")
        return self._dispatch_from_row(row)

    def claim_program_review_dispatch(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        role: RoleId | str,
        generation: Generation | int = 1,
    ) -> DispatchClaim:
        """Claim one read-only reviewer without reopening milestone execution."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        role_value = _role(role)
        generation_value = _generation(generation)
        if role_value == RoleId("executor"):
            raise ValueError("program review dispatch must use a reviewer role")
        dispatch_id = DispatchId.from_parts(run, milestone, role_value, generation_value)
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            self.get_milestone(run, milestone)
            existing = (
                self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if existing is not None:
                return self._verify_dispatch(self._dispatch_from_row(existing))
            self._require_next_dispatch_generation(run, milestone, role_value, int(generation_value))
            current = self.current_state(run, milestone)
            if current not in {
                WorkflowState.COMPLETED,
                WorkflowState.REVIEWING,
                WorkflowState.REPAIR_REQUIRED,
                WorkflowState.BLOCKED,
                WorkflowState.NEEDS_DECISION,
                WorkflowState.ACCEPTED,
            }:
                raise InvalidTransition(f"program review claim requires a completed milestone, found {current.value}")
            expected = DispatchClaim(dispatch_id, run, milestone, role_value, generation_value, now)
            self._db().execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(dispatch_id), str(run), str(milestone), str(role_value), int(generation_value), now),
            )
            return self._verify_dispatch(expected)

    def claim_program_repair_dispatch(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        generation: Generation | int = 2,
    ) -> DispatchClaim:
        """Create one bounded same-owner executor repair dispatch."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        generation_value = _generation(generation)
        if generation_value <= 1:
            raise ValueError("program repair dispatch must use a successor generation")
        dispatch_id = DispatchId.from_parts(run, milestone, RoleId("executor"), generation_value)
        now = utc_now()
        with self._transaction():
            self.get_run(run)
            existing = (
                self._db().execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if existing is not None:
                return self._verify_dispatch(self._dispatch_from_row(existing))
            owner = (
                self._db()
                .execute(
                    "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = 'executor' LIMIT 1",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if owner is None:
                raise StaleWriter("program repair requires the original executor owner")
            self._require_next_dispatch_generation(run, milestone, RoleId("executor"), int(generation_value))
            current = self.current_state(run, milestone)
            if current is not WorkflowState.REPAIR_REQUIRED:
                raise InvalidTransition(f"program repair dispatch requires REPAIR_REQUIRED, found {current.value}")
            self._db().execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) VALUES (?, ?, ?, 'executor', ?, ?)",
                (str(dispatch_id), str(run), str(milestone), int(generation_value), now),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = 'STARTING', updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (now, str(run), str(milestone)),
            )
            self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.REPAIR_REQUIRED,
                to_state=WorkflowState.STARTING,
                event_type="dispatch_claimed",
                reason=WorkflowReason(ReasonCode.DISPATCH_CLAIMED),
                dispatch_id=dispatch_id,
                data=None,
            )
            return self._verify_dispatch(
                DispatchClaim(dispatch_id, run, milestone, RoleId("executor"), generation_value, now)
            )

    # H6-E detached harness queue authority.
    @staticmethod
    def _queue_row(row: sqlite3.Row) -> JsonObject:
        return {key: row[key] for key in row.keys()}

    def enqueue_dispatch(
        self,
        dispatch_id: DispatchId | str,
        *,
        backend: str,
        capsule_json: str,
        route_json: str,
        workspace_path: Path | str,
        result_contract_sha256: str,
        action_json: str | None = None,
        available_at: str | None = None,
        deadline: str | None = None,
        plan_path: Path | str = "internal",
        plan_revision_sha256: str | None = None,
        projection_sha256: str | None = None,
        source_thread_id: str | None = None,
        permission_mode: NativePermissionMode = NativePermissionMode.READ_ONLY,
        native_profile_sha256: str | None = None,
        native_compatibility_sha256: str | None = None,
        effective_permission: NativePermissionAuthority | None = None,
        checkpoint_seconds: float = 1800.0,
        provider_env_key: str | None = None,
        profile_sha256: str | None = None,
        inspection_budget: int = 4,
        continuation_budget: int = 1,
        fresh_thread_after_idle: bool = False,
        fresh_thread_budget: int = 0,
    ) -> JsonObject:
        """Insert one immutable FIFO queue fact, idempotently."""

        dispatch = self.get_dispatch(dispatch_id)
        if backend not in {"sdk_headless", "app_native"}:
            raise ValueError("queue backend is unsupported")
        if not capsule_json or not route_json:
            raise ValueError("queue capsule and route facts must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", result_contract_sha256):
            raise ValueError("queue result contract digest is invalid")
        if result_contract_sha256 == legacy_model_facing_result_schema_sha256():
            raise ValueError("queue result contract digest is retired")
        if not isinstance(permission_mode, NativePermissionMode):
            permission_mode = NativePermissionMode(permission_mode)
        if checkpoint_seconds <= 0:
            raise ValueError("controller checkpoint duration must be positive")
        if source_thread_id is not None and (
            not source_thread_id or len(source_thread_id) > 512 or "\x00" in source_thread_id
        ):
            raise ValueError("source controller thread identity is invalid")
        actual_projection_digest = hashlib.sha256(capsule_json.encode("utf-8")).hexdigest()
        if projection_sha256 is not None and projection_sha256 != actual_projection_digest:
            raise DispatchConflict("queue projection digest does not match the generated capsule")
        projection_digest = projection_sha256 or actual_projection_digest
        plan_digest = plan_revision_sha256 or projection_digest
        _sha256(plan_digest, field_name="queue plan revision digest")
        _sha256(projection_digest, field_name="queue projection digest")
        if native_profile_sha256 is not None:
            _sha256(native_profile_sha256, field_name="queue native profile digest")
        if native_compatibility_sha256 is not None:
            _sha256(native_compatibility_sha256, field_name="queue native compatibility digest")
        if provider_env_key is not None and (
            not isinstance(provider_env_key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", provider_env_key)
        ):
            raise ValueError("provider environment key name is invalid")
        if profile_sha256 is not None:
            _sha256(profile_sha256, field_name="provider profile digest")
        if (
            isinstance(inspection_budget, bool)
            or not isinstance(inspection_budget, int)
            or not 0 <= inspection_budget <= 16
            or isinstance(continuation_budget, bool)
            or not isinstance(continuation_budget, int)
            or not 0 <= continuation_budget <= 8
            or not isinstance(fresh_thread_after_idle, bool)
            or isinstance(fresh_thread_budget, bool)
            or not isinstance(fresh_thread_budget, int)
            or not 0 <= fresh_thread_budget <= 4
            or (not fresh_thread_after_idle and fresh_thread_budget != 0)
        ):
            raise ValueError("recovery policy budgets are invalid")
        permission_json = _encode_json(effective_permission.facts) if effective_permission is not None else None
        permission_digest = (
            hashlib.sha256(permission_json.encode("utf-8")).hexdigest() if permission_json is not None else None
        )
        now = available_at or utc_now()
        if deadline is None:
            deadline = (
                datetime.fromtimestamp(
                    datetime.fromisoformat(now.removesuffix("Z")).replace(tzinfo=timezone.utc).timestamp()
                    + checkpoint_seconds,
                    timezone.utc,
                )
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        immutable = {
            "dispatch_id": str(dispatch.dispatch_id),
            "run_id": str(dispatch.run_id),
            "milestone_id": str(dispatch.milestone_id),
            "role": str(dispatch.role),
            "generation": int(dispatch.generation),
            "backend": backend,
            "capsule_json": capsule_json,
            "action_json": action_json,
            "route_json": route_json,
            "workspace_path": os.fspath(Path(workspace_path).resolve()),
            "result_contract_sha256": result_contract_sha256,
        }
        with self._transaction():
            existing = (
                self._db()
                .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch.dispatch_id),))
                .fetchone()
            )
            if existing is not None:
                row = self._queue_row(existing)
                if any(row.get(key) != value for key, value in immutable.items()):
                    raise DispatchConflict("queue dispatch already exists with different immutable facts")
                binding = (
                    self._db()
                    .execute("SELECT * FROM queue_bindings WHERE dispatch_id = ?", (str(dispatch.dispatch_id),))
                    .fetchone()
                )
                if binding is None:
                    raise CorruptSchemaError("queue dispatch is missing its v11 binding")
                expected_binding = {
                    "plan_path": os.fspath(Path(plan_path)) if isinstance(plan_path, Path) else str(plan_path),
                    "plan_revision_sha256": plan_digest,
                    "projection_sha256": projection_digest,
                    "source_thread_id": source_thread_id,
                    "permission_mode": permission_mode.value,
                    "native_profile_sha256": native_profile_sha256,
                    "native_compatibility_sha256": native_compatibility_sha256,
                    "effective_permission_json": permission_json,
                    "effective_permission_sha256": permission_digest,
                }
                if any(binding[key] != value for key, value in expected_binding.items()):
                    raise DispatchConflict("queue dispatch already exists with different v11 binding facts")
                recovery = (
                    self._db()
                    .execute(
                        "SELECT provider_env_key, profile_sha256, inspection_budget, continuation_budget, "
                        "fresh_thread_after_idle, fresh_thread_budget FROM recovery_state WHERE dispatch_id = ?",
                        (str(dispatch.dispatch_id),),
                    )
                    .fetchone()
                )
                if recovery is None:
                    raise CorruptSchemaError("queue dispatch is missing its v12 recovery state")
                expected_recovery = {
                    "provider_env_key": provider_env_key,
                    "profile_sha256": profile_sha256 or native_profile_sha256,
                    "inspection_budget": inspection_budget,
                    "continuation_budget": continuation_budget,
                    "fresh_thread_after_idle": int(fresh_thread_after_idle),
                    "fresh_thread_budget": fresh_thread_budget,
                }
                if any(recovery[key] != value for key, value in expected_recovery.items()):
                    raise DispatchConflict("queue dispatch already exists with different v12 recovery facts")
                return row
            authority = (
                self._db().execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is not None and int(authority["requested_shutdown"]) == 1:
                raise HarnessRefreshBlocked("harness refresh fence is active")
            sequence = int(
                self._db().execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM dispatch_queue").fetchone()[0]
            )
            self._db().execute(
                "INSERT INTO dispatch_queue(dispatch_id, run_id, milestone_id, role, generation, backend, capsule_json, action_json, route_json, workspace_path, result_contract_sha256, state, sequence, available_at, deadline, claim_epoch, claim_nonce_sha256, attempt, thread_id, host_id, raw_result_json, raw_result_sha256, terminal_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, 1, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    immutable["dispatch_id"],
                    immutable["run_id"],
                    immutable["milestone_id"],
                    immutable["role"],
                    immutable["generation"],
                    immutable["backend"],
                    immutable["capsule_json"],
                    immutable["action_json"],
                    immutable["route_json"],
                    immutable["workspace_path"],
                    immutable["result_contract_sha256"],
                    sequence,
                    now,
                    deadline,
                    now,
                    now,
                ),
            )
            row = (
                self._db()
                .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch.dispatch_id),))
                .fetchone()
            )
            if row is None:  # pragma: no cover
                raise LedgerError("queue insert did not produce a row")
            binding_path = os.fspath(Path(plan_path)) if isinstance(plan_path, Path) else str(plan_path)
            self._db().execute(
                "INSERT INTO queue_bindings(dispatch_id, plan_path, plan_revision_sha256, projection_sha256, "
                "source_thread_id, permission_mode, native_profile_sha256, native_compatibility_sha256, "
                "effective_permission_json, effective_permission_sha256, checkpoint_deadline, checkpoint_armed, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    str(dispatch.dispatch_id),
                    binding_path,
                    plan_digest,
                    projection_digest,
                    source_thread_id,
                    permission_mode.value,
                    native_profile_sha256,
                    native_compatibility_sha256,
                    permission_json,
                    permission_digest,
                    deadline,
                    now,
                    now,
                ),
            )
            self._db().execute(
                "INSERT INTO recovery_state(dispatch_id, provider_env_key, profile_sha256, worker_exit_classification, "
                "exit_code, recovery_state, inspection_budget, inspection_used, continuation_budget, continuation_used, "
                "fresh_thread_after_idle, fresh_thread_budget, fresh_thread_used, next_eligible_at, inspected_thread_id, "
                "inspected_turn_id, inspected_kind, human_attention_reason, exited_at, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, NULL, 'none', ?, 0, ?, 0, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    str(dispatch.dispatch_id),
                    provider_env_key,
                    profile_sha256 or native_profile_sha256,
                    inspection_budget,
                    continuation_budget,
                    int(fresh_thread_after_idle),
                    fresh_thread_budget,
                    now,
                    now,
                ),
            )
            self._db().execute(
                "INSERT INTO retry_policies(dispatch_id, revision, policy_version, pre_identity_budget, invalid_chain_budget, "
                "schema_envelope_budget, post_identity_loss_budget, pre_identity_used, invalid_chain_used, "
                "schema_envelope_used, post_identity_loss_used, last_failure, strategy, next_eligible_at, "
                "prior_thread_id, prior_turn_id, human_attention_reason, created_at, updated_at, "
                "provider_transient_budget, provider_transient_used, provider_transient_grant_used) "
                "VALUES (?, 0, 1, 5, 1, 2, 1, 0, 0, 0, 0, NULL, 'none', NULL, NULL, NULL, NULL, ?, ?, 3, 0, 0)",
                (str(dispatch.dispatch_id), now, now),
            )
            return self._queue_row(row)

    def queue_dispatch(self, dispatch_id: DispatchId | str) -> JsonObject:
        row = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        return self._queue_row(row)

    def queue_dispatches(self) -> tuple[JsonObject, ...]:
        rows = self._db().execute("SELECT * FROM dispatch_queue ORDER BY sequence").fetchall()
        return tuple(self._queue_row(row) for row in rows)

    def _control_list_entries(
        self, list_kind: ControlListKind | str, visibility: ControlListVisibility | str
    ) -> tuple[tuple[tuple[object, ...], JsonObject], ...]:
        """Return one source-owned, totally ordered control-list snapshot."""

        kind = list_kind if isinstance(list_kind, ControlListKind) else ControlListKind(list_kind)
        view = visibility if isinstance(visibility, ControlListVisibility) else ControlListVisibility(visibility)
        if kind is ControlListKind.WORKERS:
            attention_states = (
                "pending_delivery",
                "awaiting_claim",
                "claimed",
                "action_committed",
                "human_attention_required",
            )
            attention_rows = (
                self._db()
                .execute(
                    "SELECT DISTINCT dispatch_id FROM controller_decisions "
                    "WHERE program_id IS NULL AND state IN (?, ?, ?, ?, ?)",
                    attention_states,
                )
                .fetchall()
            )
            attention_dispatches = {str(row[0]) for row in attention_rows}
            rows = [self._queue_row(row) for row in self._db().execute("SELECT * FROM dispatch_queue").fetchall()]
            entries: list[tuple[tuple[object, ...], JsonObject]] = []
            for row in rows:
                state = str(row["state"])
                terminal = state in {"completed", "failed", "cancelled"}
                if (
                    view is ControlListVisibility.ACTIVE
                    and terminal
                    and str(row["dispatch_id"]) not in attention_dispatches
                ):
                    continue
                category = (
                    0
                    if state in {"claimed", "starting", "running"}
                    else 1
                    if not terminal or str(row["dispatch_id"]) in attention_dispatches
                    else 2
                )
                key = (category, -int(row["sequence"]), str(row["dispatch_id"]))
                entries.append((key, row))
            entries.sort(key=lambda item: item[0])
            return tuple(entries)

        attention_states = (
            "pending_delivery",
            "awaiting_claim",
            "claimed",
            "action_committed",
            "human_attention_required",
        )
        rows = [
            self._queue_row(row)
            for row in self._db().execute("SELECT * FROM controller_decisions WHERE program_id IS NULL").fetchall()
        ]
        entries = []
        for row in rows:
            state = str(row["state"])
            current_attention = state in attention_states
            if view is ControlListVisibility.ACTIVE and not current_attention:
                continue
            deadline = str(row["deadline"] or "")
            key = (0 if current_attention else 1, deadline, str(row["decision_id"]))
            entries.append((key, row))
        entries.sort(key=lambda item: item[0])
        return tuple(entries)

    def control_list_rows(
        self,
        list_kind: ControlListKind | str,
        visibility: ControlListVisibility | str,
        *,
        after_key: tuple[object, ...] | None = None,
    ) -> tuple[tuple[tuple[object, ...], JsonObject], ...]:
        """Expose ordered rows after one exact ephemeral continuation key."""

        entries = self._control_list_entries(list_kind, visibility)
        if after_key is None:
            return entries
        for index, (key, _row) in enumerate(entries):
            if key == after_key:
                return entries[index + 1 :]
        return ()

    def control_list_fingerprint(
        self,
        list_kind: ControlListKind | str,
        visibility: ControlListVisibility | str,
        *,
        harness_epoch: int,
    ) -> str:
        """Hash the complete ordered source identity for one page snapshot."""

        entries = self._control_list_entries(list_kind, visibility)
        kind = list_kind if isinstance(list_kind, ControlListKind) else ControlListKind(list_kind)
        view = visibility if isinstance(visibility, ControlListVisibility) else ControlListVisibility(visibility)
        source: list[list[object]] = []
        if kind is ControlListKind.WORKERS:
            for _key, row in entries:
                source.append(
                    [
                        str(row["dispatch_id"]),
                        int(row["sequence"]),
                        str(row["state"]),
                        int(row["generation"]),
                        int(row["attempt"]),
                        str(row["updated_at"]),
                    ]
                )
        else:
            for _key, row in entries:
                source.append(
                    [
                        str(row["decision_id"]),
                        str(row["state"]),
                        int(row["revision"]),
                        int(row["current_generation"]),
                        str(row["updated_at"]),
                    ]
                )
        payload = [kind.value, view.value, int(CURRENT_SCHEMA_VERSION), int(harness_epoch), source]
        return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()

    def refresh_state_inspection(self) -> JsonObject:
        """Inspect the exact fenced v19 recovery state without mutation."""

        if self.schema_version != CURRENT_SCHEMA_VERSION or self.schema_identity != _SCHEMA_IDENTITY:
            raise CorruptSchemaError("refresh state requires the canonical schema-v19 identity")
        marker = self._db().execute("SELECT value FROM schema_meta WHERE key = 'migration_marker'").fetchone()
        if marker is None or str(marker[0]) != "complete":
            raise CorruptSchemaError("refresh state requires a complete migration marker")
        tables = {
            str(row[1])
            for row in self._db().execute("SELECT type, name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "harness_authority" not in tables or "supervisor_authority" in tables:
            raise CorruptSchemaError("refresh state has an ambiguous authority table")
        authority = self.harness_authority()
        if authority is None:
            return {
                "authority": None,
                "active_dispatches": 0,
                "live_worker_leases": 0,
                "active_controller_generations": 0,
                "live_controller_claims": 0,
            }
        active_dispatches = int(
            self._db()
            .execute("SELECT COUNT(*) FROM dispatch_queue WHERE state IN ('claimed', 'starting', 'running')")
            .fetchone()[0]
        )
        live_worker_leases = int(
            self._db().execute("SELECT COUNT(*) FROM worker_liveness WHERE exited_at IS NULL").fetchone()[0]
        )
        active_controller_generations = int(
            self._db()
            .execute(
                "SELECT COUNT(*) FROM controller_decision_generations WHERE state IN ('delivery_starting', 'active')"
            )
            .fetchone()[0]
        )
        now = utc_now()
        live_controller_claims = int(
            self._db()
            .execute(
                "SELECT COUNT(*) FROM controller_decisions WHERE "
                "claimant_kind IS NOT NULL AND claimant_id IS NOT NULL "
                "AND claim_lease_expires_at IS NOT NULL AND claim_lease_expires_at > ?",
                (now,),
            )
            .fetchone()[0]
        )
        return {
            "authority": authority,
            "active_dispatches": active_dispatches,
            "live_worker_leases": live_worker_leases,
            "active_controller_generations": active_controller_generations,
            "live_controller_claims": live_controller_claims,
        }

    # H6-F live-worker diagnostics, retry policy and control-command authority.
    def append_diagnostic(
        self,
        dispatch_id: DispatchId | str,
        *,
        kind: str,
        text: str | None = None,
        payload: bytes | str | Mapping[str, object] | None = None,
        sequence: int | None = None,
        occurred_at: str | None = None,
    ) -> JsonObject:
        """Append one redacted activity item and evict oldest ring entries."""

        if not isinstance(kind, str) or not kind.strip() or len(kind) > 128 or "\x00" in kind:
            raise ValueError("diagnostic kind is invalid")
        redacted = None if text is None else redact_diagnostic_text(text)
        payload_bytes = len(redacted.encode("utf-8")) if redacted is not None else 0
        payload_digest: str | None = None
        if payload is not None:
            if isinstance(payload, Mapping):
                raw_payload = _encode_json(dict(payload)).encode("utf-8")
            elif isinstance(payload, str):
                raw_payload = payload.encode("utf-8")
            else:
                raw_payload = bytes(payload)
            if len(raw_payload) > DIAGNOSTIC_TEXT_MAX_BYTES:
                raise ValueError("diagnostic payload exceeds its byte limit")
            payload_digest = hashlib.sha256(raw_payload).hexdigest()
            # ``payload`` is the retained item's bounded body when supplied;
            # count its bytes for ring eviction even though only its digest is
            # persisted.  Worker events pass the redacted text as both fields,
            # so this remains the same count for the normal path.
            payload_bytes = len(raw_payload)
        identity = str(dispatch_id)
        with self._diagnostic_transaction():
            self._validate_diagnostic_ring_in_transaction(identity)
            row = self._append_diagnostic_in_transaction(
                identity,
                kind=kind,
                redacted=redacted,
                payload_digest=payload_digest,
                payload_bytes=payload_bytes,
                sequence=sequence,
                occurred_at=occurred_at or utc_now(),
            )
            self._validate_diagnostic_ring_in_transaction(identity)
            return self._queue_row(row)

    def append_worker_diagnostic(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        epoch: int,
        lease_seconds: float,
        kind: str,
        text: str | None,
        payload_sha256: str | None,
        sequence: int,
    ) -> JsonObject | None:
        """Renew one exact worker and append one diagnostic in the fast ring transaction."""

        self._require_exact_worker_process_identity(generation, attempt)
        if epoch <= 0 or lease_seconds <= 0:
            raise ValueError("worker diagnostic lease authority is invalid")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("diagnostic sequence is invalid")
        try:
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise StaleWriter("worker diagnostic token is invalid") from exc
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 128 or "\x00" in kind:
            raise ValueError("diagnostic kind is invalid")
        redacted = None if text is None else redact_diagnostic_text(text)
        payload_bytes = len(redacted.encode("utf-8")) if redacted is not None else 0
        if payload_sha256 is not None and (
            redacted is None
            or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
            or hashlib.sha256(redacted.encode("utf-8")).hexdigest() != payload_sha256
        ):
            raise ValueError("diagnostic payload digest conflicts with text")
        now = utc_now()
        identity = str(dispatch_id)
        with self._diagnostic_transaction():
            self._require_live_harness_in_transaction(epoch)
            self._validate_diagnostic_ring_in_transaction(identity)
            live = self._db().execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (identity,)).fetchone()
            queue = self._db().execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (identity,)).fetchone()
            capability = (
                self._db()
                .execute(
                    "SELECT token_sha256, consumed_at FROM attempt_capabilities "
                    "WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (identity, generation, attempt),
                )
                .fetchone()
            )
            if live is None or queue is None or capability is None:
                raise StaleWriter("worker diagnostic authority is unavailable")
            if (
                (int(live["generation"]), int(live["attempt"]), str(live["lease_token_sha256"]))
                != (generation, attempt, token_hash)
                or queue["state"] not in {"starting", "running"}
                or capability["consumed_at"] is not None
                or not secrets.compare_digest(str(capability["token_sha256"]), token_hash)
            ):
                raise StaleWriter("worker diagnostic authority is stale")
            self._db().execute(
                "UPDATE worker_liveness SET lease_expires_at = ?, last_seen_at = ? WHERE dispatch_id = ?",
                (self._expires_after(now, lease_seconds), now, identity),
            )
            capability_lifetime = min(
                max(float(lease_seconds), _ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS),
                _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS,
            )
            self._db().execute(
                "UPDATE attempt_capabilities SET expires_at = ? WHERE dispatch_id = ? "
                "AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                (self._expires_after(now, capability_lifetime), identity, generation, attempt),
            )
            latest = (
                self._db()
                .execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM diagnostic_ring WHERE dispatch_id = ?",
                    (identity,),
                )
                .fetchone()
            )
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM diagnostic_ring WHERE dispatch_id = ? AND sequence = ?",
                    (identity, sequence),
                )
                .fetchone()
            )
            latest_sequence = int(latest[0]) if latest is not None else 0
            if existing is not None:
                if (
                    str(existing["kind"]) != kind
                    or existing["text"] != redacted
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise StaleWriter("diagnostic replay conflicts with durable activity")
                return self._queue_row(existing)
            if sequence <= latest_sequence:
                # A bounded-ring eviction after commit can race with the
                # worker receiving its acknowledgement. The exact durable
                # stream position proves this replay was already accepted.
                return None
            if sequence != latest_sequence + 1:
                raise StaleWriter("diagnostic sequence is stale or has a gap")
            row = self._append_diagnostic_in_transaction(
                identity,
                kind=kind,
                redacted=redacted,
                payload_digest=payload_sha256,
                payload_bytes=payload_bytes,
                sequence=sequence,
                occurred_at=now,
            )
            self._validate_diagnostic_ring_in_transaction(identity)
            return self._queue_row(row)

    def _append_diagnostic_in_transaction(
        self,
        dispatch_id: str,
        *,
        kind: str,
        redacted: str | None,
        payload_digest: str | None,
        payload_bytes: int,
        sequence: int | None,
        occurred_at: str,
    ) -> sqlite3.Row:
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1):
            raise ValueError("diagnostic sequence is invalid")
        existing = (
            None
            if sequence is None
            else self._db()
            .execute("SELECT * FROM diagnostic_ring WHERE dispatch_id = ? AND sequence = ?", (dispatch_id, sequence))
            .fetchone()
        )
        if existing is not None:
            if (
                str(existing["kind"]) != kind
                or existing["text"] != redacted
                or existing["payload_sha256"] != payload_digest
            ):
                raise StaleWriter("diagnostic replay conflicts with durable activity")
            return existing
        latest = (
            self._db()
            .execute("SELECT COALESCE(MAX(sequence), 0) FROM diagnostic_ring WHERE dispatch_id = ?", (dispatch_id,))
            .fetchone()
        )
        next_sequence = int(latest[0]) + 1 if sequence is None else sequence
        if next_sequence <= int(latest[0]):
            raise StaleWriter("diagnostic sequence is stale")
        self._db().execute(
            "INSERT INTO diagnostic_ring(dispatch_id, sequence, kind, occurred_at, text, payload_sha256, payload_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dispatch_id, next_sequence, kind, occurred_at, redacted, payload_digest, payload_bytes),
        )
        while True:
            count, total = (
                self._db()
                .execute(
                    "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) FROM diagnostic_ring WHERE dispatch_id = ?",
                    (dispatch_id,),
                )
                .fetchone()
            )
            if int(count) <= DIAGNOSTIC_RING_MAX_ENTRIES and int(total) <= DIAGNOSTIC_RING_MAX_BYTES:
                break
            oldest = (
                self._db()
                .execute(
                    "SELECT sequence FROM diagnostic_ring WHERE dispatch_id = ? ORDER BY sequence LIMIT 1",
                    (dispatch_id,),
                )
                .fetchone()
            )
            if oldest is None:
                break
            self._db().execute(
                "DELETE FROM diagnostic_ring WHERE dispatch_id = ? AND sequence = ?",
                (dispatch_id, int(oldest[0])),
            )
        row = (
            self._db()
            .execute(
                "SELECT * FROM diagnostic_ring WHERE dispatch_id = ? AND sequence = ?", (dispatch_id, next_sequence)
            )
            .fetchone()
        )
        if row is None:
            raise LedgerError("diagnostic insertion was evicted unexpectedly")
        return row

    def _validate_diagnostic_ring_in_transaction(self, dispatch_id: str) -> None:
        if self._db().execute("SELECT 1 FROM dispatch_queue WHERE dispatch_id = ?", (dispatch_id,)).fetchone() is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        rows = (
            self._db()
            .execute("SELECT * FROM diagnostic_ring WHERE dispatch_id = ? ORDER BY sequence", (dispatch_id,))
            .fetchall()
        )
        if (
            len(rows) > DIAGNOSTIC_RING_MAX_ENTRIES
            or sum(int(row["payload_bytes"]) for row in rows) > DIAGNOSTIC_RING_MAX_BYTES
        ):
            raise CorruptSchemaError("diagnostic ring exceeds its bound")
        sequences = [int(row["sequence"]) for row in rows]
        if sequences != sorted(set(sequences)):
            raise CorruptSchemaError("diagnostic ring sequence is not monotonic")
        for row in rows:
            text = row["text"]
            if int(row["payload_bytes"]) > DIAGNOSTIC_TEXT_MAX_BYTES:
                raise CorruptSchemaError("diagnostic payload exceeds its byte bound")
            if text is not None and (
                len(str(text).encode("utf-8")) > DIAGNOSTIC_TEXT_MAX_BYTES
                or redact_diagnostic_text(str(text)) != str(text)
            ):
                raise CorruptSchemaError("diagnostic text is unredacted or oversized")
            digest = row["payload_sha256"]
            if digest is not None and re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None:
                raise CorruptSchemaError("diagnostic payload digest is malformed")

    def recent_activity(
        self, dispatch_id: DispatchId | str, *, limit: int = DIAGNOSTIC_RING_MAX_ENTRIES
    ) -> tuple[JsonObject, ...]:
        """Read the bounded diagnostic ring without mutating lifecycle state."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= DIAGNOSTIC_RING_MAX_ENTRIES:
            raise ValueError("recent activity limit is invalid")
        if (
            self._db().execute("SELECT 1 FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            is None
        ):
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        rows = (
            self._db()
            .execute(
                "SELECT * FROM diagnostic_ring WHERE dispatch_id = ? ORDER BY sequence DESC LIMIT ?",
                (str(dispatch_id), limit),
            )
            .fetchall()
        )
        return tuple(self._queue_row(row) for row in reversed(rows))

    def retry_policy(self, dispatch_id: DispatchId | str) -> JsonObject:
        row = self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise RecordNotFound(f"retry policy does not exist: {dispatch_id}")
        return self._queue_row(row)

    def record_retry_failure(
        self,
        dispatch_id: DispatchId | str,
        *,
        failure: RetryFailureClass | str,
        prior_thread_id: str | None = None,
        prior_turn_id: str | None = None,
        next_eligible_at: str | None = None,
        attention_reason: str | None = None,
    ) -> JsonObject:
        """Atomically consume one typed retry budget or require attention."""

        target = failure if isinstance(failure, RetryFailureClass) else RetryFailureClass(failure)
        if prior_thread_id is not None:
            ThreadIdentity(prior_thread_id)
        if prior_turn_id is not None:
            if (
                not isinstance(prior_turn_id, str)
                or not prior_turn_id
                or len(prior_turn_id) > 512
                or any(c.isspace() for c in prior_turn_id)
            ):
                raise ValueError("prior turn identity is invalid")
        if next_eligible_at is not None:
            if (
                not isinstance(next_eligible_at, str)
                or not next_eligible_at
                or len(next_eligible_at) > 64
                or "\x00" in next_eligible_at
            ):
                raise ValueError("retry eligibility timestamp is invalid")
            try:
                parsed_eligibility = datetime.fromisoformat(
                    next_eligible_at[:-1] + "+00:00" if next_eligible_at.endswith("Z") else next_eligible_at
                )
            except ValueError as exc:
                raise ValueError("retry eligibility timestamp is invalid") from exc
            if parsed_eligibility.tzinfo is None:
                raise ValueError("retry eligibility timestamp must be timezone-aware")
        now = utc_now()
        retryable = target in {
            RetryFailureClass.PRE_IDENTITY_TRANSPORT,
            RetryFailureClass.INVALID_RESPONSE_CHAIN,
            RetryFailureClass.SCHEMA_ENVELOPE,
            RetryFailureClass.POST_IDENTITY_LOSS,
            RetryFailureClass.PROVIDER_TRANSIENT,
        }
        column_by_failure = {
            RetryFailureClass.PRE_IDENTITY_TRANSPORT: (
                "pre_identity_used",
                "pre_identity_budget",
                RecoveryStrategy.BACKOFF,
            ),
            RetryFailureClass.INVALID_RESPONSE_CHAIN: (
                "invalid_chain_used",
                "invalid_chain_budget",
                RecoveryStrategy.FRESH_THREAD,
            ),
            RetryFailureClass.SCHEMA_ENVELOPE: (
                "schema_envelope_used",
                "schema_envelope_budget",
                RecoveryStrategy.SAME_THREAD_SCHEMA_CORRECTION,
            ),
            RetryFailureClass.POST_IDENTITY_LOSS: (
                "post_identity_loss_used",
                "post_identity_loss_budget",
                RecoveryStrategy.SAME_THREAD_CONTINUATION,
            ),
            RetryFailureClass.PROVIDER_TRANSIENT: (
                "provider_transient_used",
                "provider_transient_budget",
                RecoveryStrategy.SAME_THREAD_CONTINUATION,
            ),
        }
        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            queue = (
                self._db()
                .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if row is None or queue is None:
                raise RecordNotFound(f"retry policy does not exist: {dispatch_id}")
            if str(queue["state"]) in {"completed", "failed", "cancelled", "result_submitted", "finalizing"}:
                raise StaleWriter("terminal queue dispatch rejects retry failure")
            if str(row["strategy"]) == RecoveryStrategy.HUMAN_ATTENTION.value:
                return self._queue_row(row)
            next_state = RecoveryStrategy.HUMAN_ATTENTION
            used_column = budget_column = None
            if retryable:
                used_column, budget_column, next_state = column_by_failure[target]
                if int(row[used_column]) < int(row[budget_column]):
                    used = int(row[used_column]) + 1
                    if next_eligible_at is None and next_state in {
                        RecoveryStrategy.BACKOFF,
                        RecoveryStrategy.SAME_THREAD_CONTINUATION,
                    }:
                        # Deterministic bounded jitter avoids synchronized
                        # retries without making tests or recovery opaque.
                        jitter = int(hashlib.sha256(f"{dispatch_id}:{used}".encode()).hexdigest()[:4], 16) / 65535
                        next_eligible_at = self._expires_after(now, min(300.0, (2 ** (used - 1)) + jitter))
                    self._db().execute(
                        f"UPDATE retry_policies SET {used_column} = ?, revision = revision + 1, last_failure = ?, strategy = ?, next_eligible_at = ?, prior_thread_id = ?, prior_turn_id = ?, updated_at = ? WHERE dispatch_id = ?",
                        (
                            used,
                            target.value,
                            next_state.value,
                            next_eligible_at,
                            prior_thread_id,
                            prior_turn_id,
                            now,
                            str(dispatch_id),
                        ),
                    )
                    if target is RetryFailureClass.INVALID_RESPONSE_CHAIN:
                        # The provider has rejected the continuation chain;
                        # after the required read-only inspection, the one
                        # allowed recovery must start a fresh SDK thread in
                        # the same workspace rather than replaying the
                        # unusable previous chain.
                        self._db().execute(
                            "UPDATE recovery_state SET fresh_thread_after_idle = 1, "
                            "fresh_thread_budget = MAX(fresh_thread_budget, 1), updated_at = ? "
                            "WHERE dispatch_id = ? AND fresh_thread_used = 0",
                            (now, str(dispatch_id)),
                        )
                    if target is RetryFailureClass.PROVIDER_TRANSIENT:
                        # The common recovery continuation budget is raised
                        # only to the durable provider ceiling.  This is a
                        # monotonic sufficiency repair, never a counter reset.
                        self._db().execute(
                            "UPDATE recovery_state SET continuation_budget = MAX(continuation_budget, ?), updated_at = ? "
                            "WHERE dispatch_id = ?",
                            (int(row[budget_column]), now, str(dispatch_id)),
                        )
                    # A pre-identity transport failure has no SDK thread to
                    # inspect.  Once its worker exit is durably observed, it
                    # can return directly to the FIFO queue with the typed
                    # backoff and a fresh process attempt.  Never perform
                    # this transition while a worker lease is still live.
                    if target is RetryFailureClass.PRE_IDENTITY_TRANSPORT:
                        live = (
                            self._db()
                            .execute(
                                "SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?",
                                (str(dispatch_id),),
                            )
                            .fetchone()
                        )
                        if str(queue["state"]) != "queued" and (live is None or live["exited_at"] is not None):
                            available = next_eligible_at or now
                            self._db().execute(
                                "DELETE FROM attempt_capabilities WHERE dispatch_id = ?",
                                (str(dispatch_id),),
                            )
                            self._db().execute(
                                "DELETE FROM worker_liveness WHERE dispatch_id = ?",
                                (str(dispatch_id),),
                            )
                            self._db().execute(
                                "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, "
                                "inspected_thread_id = NULL, inspected_turn_id = NULL, inspected_kind = NULL, "
                                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                                (now, str(dispatch_id)),
                            )
                            self._db().execute(
                                "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, "
                                "claim_nonce_sha256 = NULL, attempt = attempt + 1, thread_id = NULL, host_id = NULL, "
                                "available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                                (available, now, str(dispatch_id)),
                            )
                    elif str(queue["state"]) == "queued" and next_state is RecoveryStrategy.BACKOFF:
                        # Queue claiming must honor the same durable backoff
                        # even when a caller records a failure before a
                        # process claim exists.
                        self._db().execute(
                            "UPDATE dispatch_queue SET available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                            (next_eligible_at or now, now, str(dispatch_id)),
                        )
                    updated = (
                        self._db()
                        .execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),))
                        .fetchone()
                    )
                    return self._queue_row(updated)
            reason = attention_reason or f"retry budget exhausted for {target.value}"
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 512
                or len(reason.encode("utf-8")) > 512
                or "\x00" in reason
            ):
                raise ValueError("retry attention reason is invalid")
            reason = redact_control_text(reason, limit=512)
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, last_failure = ?, strategy = ?, next_eligible_at = NULL, prior_thread_id = ?, prior_turn_id = ?, human_attention_reason = ?, updated_at = ? WHERE dispatch_id = ?",
                (
                    target.value,
                    RecoveryStrategy.HUMAN_ATTENTION.value,
                    prior_thread_id,
                    prior_turn_id,
                    reason,
                    now,
                    str(dispatch_id),
                ),
            )
            if str(queue["state"]) not in {"completed", "failed", "cancelled"}:
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'human_attention_required', updated_at = ? WHERE dispatch_id = ?",
                    (now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'human_attention_required', "
                    "next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    (reason, now, str(dispatch_id)),
                )
            return self._queue_row(
                self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def retry_dispatch(
        self,
        dispatch_id: DispatchId | str,
        *,
        expected_strategy: RecoveryStrategy | str,
        reason: str,
        expected_state: str = "human_attention_required",
    ) -> JsonObject:
        """Apply one explicit compare-and-swap human/controller retry decision."""

        strategy = (
            expected_strategy
            if isinstance(expected_strategy, RecoveryStrategy)
            else RecoveryStrategy(expected_strategy)
        )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512 or "\x00" in reason:
            raise ValueError("retry decision reason is invalid")
        try:
            if len(reason.encode("utf-8")) > 512:
                raise ValueError("retry decision reason is invalid")
            reason = redact_control_text(reason, limit=512)
        except UnicodeEncodeError as exc:
            raise ValueError("retry decision reason is invalid") from exc
        with self._transaction():
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            policy = (
                self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None or policy is None:
                raise RecordNotFound(f"retry dispatch does not exist: {dispatch_id}")
            if str(queue["state"]) != expected_state or str(policy["strategy"]) != strategy.value:
                raise StaleWriter("retry decision compare-and-swap precondition failed")
            now = utc_now()
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', human_attention_reason = NULL, next_eligible_at = ?, updated_at = ? WHERE dispatch_id = ?",
                (now, now, str(dispatch_id)),
            )
            self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute(
                "UPDATE control_commands SET state = 'unresolved', acknowledged_at = COALESCE(acknowledged_at, ?) "
                "WHERE dispatch_id = ? AND state IN ('pending', 'sent')",
                (now, str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, claim_nonce_sha256 = NULL, "
                "attempt = attempt + 1, thread_id = NULL, host_id = NULL, available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                (now, now, str(dispatch_id)),
            )
            # The compare-and-swap decision has released a fresh queued
            # attempt.  Keep recovery state aligned with that queue state;
            # retaining ``human_attention_required`` here would violate the
            # queue/recovery invariant at transaction commit.  The bounded,
            # redacted reason is an authorization input, not a terminal
            # recovery fact, so it must not be copied into a non-attention row.
            self._db().execute(
                "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, "
                "inspected_thread_id = NULL, inspected_turn_id = NULL, inspected_kind = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (now, str(dispatch_id)),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def create_control_command(
        self,
        dispatch_id: DispatchId | str,
        *,
        command_id: str,
        generation: int,
        attempt: int,
        thread_id: str,
        turn_id: str,
        kind: ControlCommandKind | str,
        payload: str | None = None,
    ) -> JsonObject:
        """Persist one exact-turn steer/interrupt command idempotently."""

        command_id = _command_id(command_id)
        target = kind if isinstance(kind, ControlCommandKind) else ControlCommandKind(kind)
        normalized_payload = None if payload is None else redact_control_text(payload)
        digest = hashlib.sha256((normalized_payload or "").encode("utf-8")).hexdigest()
        with self._transaction():
            existing = (
                self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            )
            if existing is not None:
                if any(
                    existing[key] != value
                    for key, value in {
                        "dispatch_id": str(dispatch_id),
                        "generation": generation,
                        "attempt": attempt,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "kind": target.value,
                        "payload": normalized_payload,
                        "payload_sha256": digest,
                    }.items()
                ):
                    raise StaleWriter("control command identity is conflicting")
                return self._queue_row(existing)
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if str(queue["state"]) in {
                "result_submitted",
                "finalizing",
                "completed",
                "failed",
                "cancelled",
                "human_attention_required",
            }:
                raise StaleWriter("terminal queue dispatch rejects control commands")
            if (int(queue["generation"]), int(queue["attempt"]), queue["thread_id"]) != (
                generation,
                attempt,
                thread_id,
            ):
                raise StaleWriter("control command exact-turn identity is stale")
            next_sequence = int(
                self._db()
                .execute(
                    "SELECT COALESCE(MAX(submission_sequence), 0) + 1 FROM control_commands WHERE dispatch_id = ?",
                    (str(dispatch_id),),
                )
                .fetchone()[0]
            )
            ControlCommand(
                command_id=command_id,
                dispatch_id=DispatchId(dispatch_id),
                generation=Generation(generation),
                attempt=attempt,
                thread_id=ThreadIdentity(thread_id),
                turn_id=turn_id,
                kind=target,
                payload=normalized_payload,
                payload_sha256=digest,
                submission_sequence=next_sequence,
            )
            now = utc_now()
            self._db().execute(
                "INSERT INTO control_commands(command_id, dispatch_id, submission_sequence, generation, attempt, thread_id, turn_id, kind, payload, payload_sha256, state, acknowledgement_json, created_at, sent_at, acknowledged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL, NULL)",
                (
                    command_id,
                    str(dispatch_id),
                    next_sequence,
                    generation,
                    attempt,
                    thread_id,
                    turn_id,
                    target.value,
                    normalized_payload,
                    digest,
                    now,
                ),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            )

    def control_command(self, command_id: str) -> JsonObject:
        command_id = _command_id(command_id)
        row = self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
        if row is None:
            raise RecordNotFound(f"control command does not exist: {command_id}")
        return self._queue_row(row)

    def control_commands(self, dispatch_id: DispatchId | str) -> tuple[JsonObject, ...]:
        rows = (
            self._db()
            .execute(
                "SELECT * FROM control_commands WHERE dispatch_id = ? ORDER BY submission_sequence",
                (str(dispatch_id),),
            )
            .fetchall()
        )
        return tuple(self._queue_row(row) for row in rows)

    def pending_control_commands(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        thread_id: str,
        turn_id: str,
    ) -> tuple[JsonObject, ...]:
        """Return pending commands for one exact live-turn identity."""

        rows = (
            self._db()
            .execute(
                "SELECT * FROM control_commands WHERE dispatch_id = ? AND generation = ? AND attempt = ? "
                "AND thread_id = ? AND turn_id = ? AND state = 'pending' ORDER BY submission_sequence",
                (str(dispatch_id), generation, attempt, thread_id, turn_id),
            )
            .fetchall()
        )
        return tuple(self._queue_row(row) for row in rows)

    def claim_control_commands(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        thread_id: str,
        turn_id: str,
    ) -> tuple[JsonObject, ...]:
        """Mark and return one exact-turn command batch as sent atomically."""

        with self._transaction():
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM control_commands WHERE dispatch_id = ? AND generation = ? AND attempt = ? "
                    "AND thread_id = ? AND turn_id = ? AND state = 'pending' ORDER BY submission_sequence",
                    (str(dispatch_id), generation, attempt, thread_id, turn_id),
                )
                .fetchall()
            )
            if rows:
                self._db().execute(
                    "UPDATE control_commands SET state = 'sent', sent_at = COALESCE(sent_at, ?) "
                    "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND thread_id = ? AND turn_id = ? AND state = 'pending'",
                    (utc_now(), str(dispatch_id), generation, attempt, thread_id, turn_id),
                )
            updated = (
                self._db()
                .execute(
                    "SELECT * FROM control_commands WHERE dispatch_id = ? AND generation = ? AND attempt = ? "
                    "AND thread_id = ? AND turn_id = ? AND state = 'sent' ORDER BY submission_sequence",
                    (str(dispatch_id), generation, attempt, thread_id, turn_id),
                )
                .fetchall()
            )
            return tuple(self._queue_row(row) for row in updated)

    def unresolved_control_commands(self, dispatch_id: DispatchId | str) -> int:
        """Mark pending/sent commands unresolved when their live turn is lost."""

        with self._transaction():
            now = utc_now()
            cursor = self._db().execute(
                "UPDATE control_commands SET state = 'unresolved', acknowledged_at = COALESCE(acknowledged_at, ?) "
                "WHERE dispatch_id = ? AND state IN ('pending', 'sent')",
                (now, str(dispatch_id)),
            )
            return int(cursor.rowcount)

    def acknowledge_control_command(
        self,
        command_id: str,
        *,
        state: ControlCommandState | str,
        acknowledgement: Mapping[str, object] | None = None,
    ) -> JsonObject:
        command_id = _command_id(command_id)
        target = state if isinstance(state, ControlCommandState) else ControlCommandState(state)
        if target not in {
            ControlCommandState.ACKNOWLEDGED,
            ControlCommandState.REJECTED,
            ControlCommandState.UNRESOLVED,
        }:
            raise ValueError("control acknowledgement state is invalid")
        if target is ControlCommandState.ACKNOWLEDGED and acknowledgement is None:
            acknowledgement = {"detail": None}
        if acknowledgement is not None:
            try:
                entries = tuple(acknowledgement.items())
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("control acknowledgement must be a mapping") from exc
            if {key for key, _value in entries} not in ({"detail"}, {"detail", "terminal_status"}):
                raise ValueError("control acknowledgement has an unsupported shape")
            safe_ack: dict[str, object] = {}
            for key, value in entries:
                if key == "detail":
                    if value is None:
                        safe_ack[key] = None
                    elif isinstance(value, str):
                        safe_ack[key] = redact_control_text(value, limit=512)
                    else:
                        raise ValueError("control acknowledgement detail is invalid")
                elif key == "terminal_status" and value in CONTROL_ACKNOWLEDGEMENT_TERMINAL_STATUSES:
                    safe_ack[key] = value
                else:
                    raise ValueError("control acknowledgement terminal status is invalid")
            acknowledgement = safe_ack
        encoded = None if acknowledgement is None else _encode_json(dict(acknowledgement))
        if encoded is not None and len(encoded.encode("utf-8")) > 4096:
            raise ValueError("control acknowledgement is oversized")
        with self._transaction():
            row = self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise RecordNotFound(f"control command does not exist: {command_id}")
            expected_identity = {
                "dispatch_id": str(row["dispatch_id"]),
                "generation": int(row["generation"]),
                "attempt": int(row["attempt"]),
                "thread_id": str(row["thread_id"]),
                "turn_id": str(row["turn_id"]),
                "kind": str(row["kind"]),
            }
            if acknowledgement is not None:
                for key, value in expected_identity.items():
                    if key in acknowledgement and acknowledgement[key] != value:
                        raise StaleWriter("control acknowledgement identity conflicts")
            if row["state"] in {
                ControlCommandState.ACKNOWLEDGED.value,
                ControlCommandState.REJECTED.value,
                ControlCommandState.UNRESOLVED.value,
            }:
                if target.value != row["state"] or encoded != row["acknowledgement_json"]:
                    raise StaleWriter("control acknowledgement is immutable")
                return self._queue_row(row)
            if row["state"] != ControlCommandState.SENT.value:
                # A worker can only acknowledge a command after the
                # harness atomically moved it from pending to sent.  This
                # closes forged pre-poll acknowledgements and keeps replay
                # semantics explicit.
                raise StaleWriter("control acknowledgement requires a sent command")
            now = utc_now()
            self._db().execute(
                "UPDATE control_commands SET state = ?, acknowledgement_json = ?, acknowledged_at = ?, sent_at = COALESCE(sent_at, ?) WHERE command_id = ? AND state IN ('pending','sent','rejected','unresolved')",
                (target.value, encoded, now, now, command_id),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            )

    def mark_control_command_sent(self, command_id: str) -> JsonObject:
        command_id = _command_id(command_id)
        with self._transaction():
            row = self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            if row is None:
                raise RecordNotFound(f"control command does not exist: {command_id}")
            if row["state"] in {
                ControlCommandState.ACKNOWLEDGED.value,
                ControlCommandState.REJECTED.value,
                ControlCommandState.UNRESOLVED.value,
            }:
                return self._queue_row(row)
            self._db().execute(
                "UPDATE control_commands SET state = 'sent', sent_at = COALESCE(sent_at, ?) WHERE command_id = ?",
                (utc_now(), command_id),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM control_commands WHERE command_id = ?", (command_id,)).fetchone()
            )

    def claim_queue_dispatch(self, *, epoch: int, claim_nonce_sha256: str) -> JsonObject | None:
        if epoch <= 0 or not re.fullmatch(r"[0-9a-f]{64}", claim_nonce_sha256):
            raise ValueError("queue claim authority is invalid")
        now = utc_now()
        with self._transaction():
            authority = (
                self._db()
                .execute("SELECT epoch, expires_at, requested_shutdown FROM harness_authority WHERE singleton = 1")
                .fetchone()
            )
            if authority is None or int(authority["epoch"]) != epoch or str(authority["expires_at"]) <= now:
                raise StaleWriter("queue claim requires a live harness lease")
            if int(authority["requested_shutdown"]) == 1:
                raise HarnessRefreshBlocked("harness refresh fence is active")
            row = (
                self._db()
                .execute(
                    "SELECT q.*, x.status AS execution_status FROM dispatch_queue q "
                    "LEFT JOIN executions x ON x.run_id = q.run_id AND x.milestone_id = q.milestone_id "
                    "WHERE q.state = 'queued' AND q.available_at <= ? "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM retry_policies rp "
                    "WHERE rp.dispatch_id = q.dispatch_id AND rp.strategy = 'backoff' "
                    "AND rp.next_eligible_at IS NOT NULL AND rp.next_eligible_at > ?"
                    ") "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM authorized_successors a "
                    "WHERE a.successor_dispatch_id = q.dispatch_id AND a.released_at IS NULL"
                    ") ORDER BY q.sequence LIMIT 1",
                    (now, now),
                )
                .fetchone()
            )
            if row is None:
                return None
            if row["execution_status"] == ExecutionStatus.CANCELLED.value:
                self._cancel_queue_row_in_transaction(row)
                return None
            dispatch_id = str(row["dispatch_id"])
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'claimed', claim_epoch = ?, claim_nonce_sha256 = ?, updated_at = ? WHERE dispatch_id = ? AND state = 'queued'",
                (epoch, claim_nonce_sha256, now, dispatch_id),
            )
            row = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
            return self._queue_row(row) if row is not None else None

    def set_queue_state(self, dispatch_id: DispatchId | str, state: str, *, epoch: int | None = None) -> JsonObject:
        allowed = {
            "queued",
            "claimed",
            "starting",
            "running",
            "recovery_inspection_pending",
            "recovery_retry_wait",
            "recovery_continuation_pending",
            "human_attention_required",
            "result_submitted",
            "finalizing",
            "completed",
            "failed",
            "cancelled",
        }
        if state not in allowed:
            raise ValueError("unsupported queue state")
        if state == "cancelled":
            return self.cancel_queue_dispatch(dispatch_id, epoch=epoch)
        transitions = {
            "queued": {"queued", "claimed", "cancelled"},
            "claimed": {"claimed", "starting", "queued", "cancelled"},
            "starting": {"starting", "running", "queued", "result_submitted", "failed", "cancelled"},
            "running": {"running", "result_submitted", "queued", "failed", "cancelled"},
            "recovery_inspection_pending": {
                "recovery_inspection_pending",
                "recovery_retry_wait",
                "recovery_continuation_pending",
                "completed",
                "failed",
                "human_attention_required",
                "queued",
                "cancelled",
            },
            "recovery_retry_wait": {
                "recovery_retry_wait",
                "recovery_inspection_pending",
                "human_attention_required",
                "completed",
                "failed",
                "queued",
                "cancelled",
            },
            "recovery_continuation_pending": {
                "recovery_continuation_pending",
                "recovery_inspection_pending",
                "completed",
                "failed",
                "human_attention_required",
                "queued",
                "cancelled",
            },
            "human_attention_required": {"human_attention_required"},
            "result_submitted": {"result_submitted", "finalizing", "completed", "failed"},
            "finalizing": {"finalizing", "completed", "failed"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if epoch is not None and row["claim_epoch"] != epoch:
                raise StaleWriter("queue dispatch claim epoch is stale")
            if state not in transitions[str(row["state"])] or (
                row["state"] in {"completed", "failed", "cancelled"} and row["state"] != state
            ):
                raise StaleWriter(f"queue state transition {row['state']} -> {state} is not allowed")
            if epoch is not None:
                authority = (
                    self._db().execute("SELECT epoch, expires_at FROM harness_authority WHERE singleton = 1").fetchone()
                )
                if (
                    authority is None
                    or int(authority["epoch"]) != int(epoch)
                    or str(authority["expires_at"]) <= utc_now()
                ):
                    raise StaleWriter("queue state transition requires a live harness lease")
            self._db().execute(
                "UPDATE dispatch_queue SET state = ?, updated_at = ? WHERE dispatch_id = ?",
                (state, utc_now(), str(dispatch_id)),
            )
            updated = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            return self._queue_row(updated)

    def _cancel_queue_row_in_transaction(
        self,
        row: sqlite3.Row,
        *,
        epoch: int | None = None,
        retain_exited_liveness: bool = False,
    ) -> JsonObject:
        """Terminalize an active queue row without inventing a model result."""

        state = str(row["state"])
        if state == "cancelled":
            return self._queue_row(row)
        if state in {"result_submitted", "finalizing", "completed", "failed"}:
            raise StaleWriter(f"{state} queue dispatch cannot be cancelled")
        if state not in {
            "queued",
            "claimed",
            "starting",
            "running",
            "recovery_inspection_pending",
            "recovery_retry_wait",
            "recovery_continuation_pending",
            "human_attention_required",
        }:
            raise StaleWriter(f"queue dispatch cannot be cancelled from state {state}")
        if epoch is not None:
            if row["claim_epoch"] != epoch:
                raise StaleWriter("queue cancellation claim epoch is stale")
            authority = (
                self._db().execute("SELECT epoch, expires_at FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is None or int(authority["epoch"]) != int(epoch) or str(authority["expires_at"]) <= utc_now():
                raise StaleWriter("queue cancellation requires a live harness lease")
        if (
            row["raw_result_json"] is not None
            or row["raw_result_sha256"] is not None
            or row["terminal_status"] is not None
        ):
            raise CorruptSchemaError("active queue dispatch carries terminal result facts")
        for table, column in (("successor_outbox", "source_dispatch_id"), ("notification_outbox", "dispatch_id")):
            if self._db().execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (row["dispatch_id"],)).fetchone():
                raise CorruptSchemaError("active queue dispatch carries terminal outbox facts")
        now = utc_now()
        self._db().execute(
            "UPDATE control_commands SET state = 'unresolved', acknowledged_at = COALESCE(acknowledged_at, ?) "
            "WHERE dispatch_id = ? AND state IN ('pending', 'sent')",
            (now, row["dispatch_id"]),
        )
        self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (row["dispatch_id"],))
        if retain_exited_liveness:
            live = (
                self._db()
                .execute("SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?", (row["dispatch_id"],))
                .fetchone()
            )
            if live is None or live["exited_at"] is None:
                raise StaleWriter("worker cancellation lacks durable exit acknowledgement")
        else:
            self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (row["dispatch_id"],))
        self._db().execute(
            "UPDATE dispatch_queue SET state = 'cancelled', claim_epoch = NULL, claim_nonce_sha256 = NULL, "
            "raw_result_json = NULL, raw_result_sha256 = NULL, terminal_status = NULL, updated_at = ? "
            "WHERE dispatch_id = ? AND state IN ('queued', 'claimed', 'starting', 'running', 'recovery_inspection_pending', 'recovery_retry_wait', 'recovery_continuation_pending', 'human_attention_required')",
            (now, row["dispatch_id"]),
        )
        self._db().execute(
            "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', next_eligible_at = NULL, "
            "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
            (now, row["dispatch_id"]),
        )
        self._db().execute(
            "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, inspected_thread_id = NULL, "
            "inspected_turn_id = NULL, inspected_kind = NULL, human_attention_reason = NULL, updated_at = ? "
            "WHERE dispatch_id = ?",
            (now, row["dispatch_id"]),
        )
        # Cancellation is deliberately resultless and emits no terminal wake.
        # Retire any outstanding checkpoint decision before removing its
        # delivery envelope so a failed or ambiguous checkpoint cannot keep a
        # cancelled dispatch structurally live.
        self._db().execute(
            "UPDATE controller_decisions SET state = 'superseded', superseded_at = ?, "
            "claim_lease_expires_at = NULL, human_attention_reason = NULL, updated_at = ? "
            "WHERE dispatch_id = ? AND kind = 'checkpoint' "
            "AND state NOT IN ('action_committed', 'acknowledged', 'superseded', 'legacy_closed')",
            (now, now, row["dispatch_id"]),
        )
        self._db().execute(
            "DELETE FROM wake_outbox WHERE dispatch_id = ? AND kind = 'checkpoint'",
            (row["dispatch_id"],),
        )
        updated = (
            self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (row["dispatch_id"],)).fetchone()
        )
        if updated is None:  # pragma: no cover - foreign-key ownership makes this unreachable
            raise LedgerError("queue cancellation removed its dispatch")
        return self._queue_row(updated)

    def cancel_queue_dispatch(self, dispatch_id: DispatchId | str, *, epoch: int | None = None) -> JsonObject:
        """Cancel one queue dispatch exactly once, with no result or outboxes."""

        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            return self._cancel_queue_row_in_transaction(row, epoch=epoch)

    def pending_cancellation_actions(self) -> tuple[JsonObject, ...]:
        """Return reasoned cancellations whose exact worker stop is not yet acknowledged."""

        rows = (
            self._db()
            .execute(
                "SELECT a.* FROM recovery_controls a JOIN dispatch_queue q ON q.dispatch_id = a.dispatch_id "
                "WHERE a.action_kind = 'cancel' AND q.state != 'cancelled' ORDER BY a.created_at, a.action_id"
            )
            .fetchall()
        )
        return tuple(self._queue_row(row) for row in rows)

    def acknowledge_worker_cancellation_exit(
        self,
        dispatch_id: DispatchId | str,
        *,
        action_id: str,
        pid: int,
        process_birth_identity: str,
        exit_code: int | None,
    ) -> JsonObject:
        """Atomically bind the exact child exit and terminal cancellation."""

        action_id = _command_id(action_id)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not process_birth_identity:
            raise ValueError("worker cancellation exit identity is invalid")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("worker cancellation exit code is invalid")
        with self._transaction():
            action = self._db().execute("SELECT * FROM recovery_controls WHERE action_id = ?", (action_id,)).fetchone()
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if action is None or queue is None:
                raise RecordNotFound(f"worker cancellation does not exist: {dispatch_id}")
            if str(action["dispatch_id"]) != str(dispatch_id) or action["action_kind"] != "cancel":
                raise StaleWriter("worker cancellation action identity conflicts")
            if queue["state"] == "cancelled":
                return self._queue_row(queue)
            live = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if live is None or (int(live["pid"]), str(live["process_birth_identity"])) != (
                pid,
                process_birth_identity,
            ):
                raise StaleWriter("worker cancellation exit identity is stale")
            now = utc_now()
            if live["exited_at"] is None:
                self._db().execute(
                    "UPDATE worker_liveness SET exited_at = ?, last_seen_at = ? WHERE dispatch_id = ?",
                    (now, now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE recovery_state SET worker_exit_classification = 'harness-cancelled', exit_code = ?, "
                    "exited_at = ?, updated_at = ? WHERE dispatch_id = ?",
                    (exit_code, now, now, str(dispatch_id)),
                )
            return self._cancel_queue_row_in_transaction(queue, retain_exited_liveness=True)

    def recover_queue_dispatch(
        self,
        dispatch_id: DispatchId | str,
        *,
        new_epoch: int,
        preserve_thread_id: bool = False,
    ) -> JsonObject:
        """Invalidate a stale attempt and return one crashed claim to FIFO queueing."""

        if new_epoch <= 0:
            raise ValueError("recovery epoch is invalid")
        with self._transaction():
            authority = (
                self._db().execute("SELECT epoch, expires_at FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is None or int(authority["epoch"]) != new_epoch or str(authority["expires_at"]) <= utc_now():
                raise StaleWriter("queue recovery requires a live harness lease")
            row = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if row["state"] in {"completed", "failed", "cancelled", "queued"}:
                return self._queue_row(row)
            if row["state"] in {
                "recovery_inspection_pending",
                "recovery_retry_wait",
                "recovery_continuation_pending",
                "human_attention_required",
            }:
                raise StaleWriter("recovery-state dispatch requires its typed recovery action")
            # Retire the old attempt atomically with the queue requeue.  A
            # replayed capability then has no matching durable fact.
            self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, claim_nonce_sha256 = NULL, "
                "attempt = attempt + 1, thread_id = ?, host_id = NULL, updated_at = ? WHERE dispatch_id = ?",
                (row["thread_id"] if preserve_thread_id else None, utc_now(), str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, "
                "inspected_thread_id = NULL, inspected_turn_id = NULL, inspected_kind = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (utc_now(), str(dispatch_id)),
            )
            updated = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            return self._queue_row(updated)

    def issue_attempt_capability(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        operation: str,
        schema_sha256: str,
        workspace_path: Path | str,
        backend: str,
        token_sha256: str,
        expires_at: str | None = None,
        lease_seconds: float = _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS,
    ) -> JsonObject:
        if operation != "submit_result" or backend not in {"sdk_headless", "app_native"}:
            raise ValueError("unsupported capability operation or backend")
        if not re.fullmatch(r"[0-9a-f]{64}", schema_sha256) or not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
            raise ValueError("capability digest is invalid")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int | float)
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("capability lease duration is invalid")
        resolved_workspace = os.fspath(Path(workspace_path).resolve())
        with self._transaction():
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if (
                generation != int(queue["generation"])
                or attempt != int(queue["attempt"])
                or backend != str(queue["backend"])
                or resolved_workspace != str(queue["workspace_path"])
                or schema_sha256 != str(queue["result_contract_sha256"])
            ):
                raise StaleWriter("attempt capability does not match queue binding")
            now = utc_now()
            now_value = datetime.fromisoformat(now.removesuffix("Z")).replace(tzinfo=timezone.utc)
            bounded_seconds = min(
                max(float(lease_seconds), _ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS),
                _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS,
            )
            minimum_expiry = self._expires_after(now, bounded_seconds)
            maximum_expiry = self._expires_after(now, _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS)
            if expires_at is None:
                capability_expiry = minimum_expiry
            else:
                if not isinstance(expires_at, str) or not expires_at:
                    raise ValueError("capability expiry is invalid")
                try:
                    requested_value = datetime.fromisoformat(expires_at.removesuffix("Z"))
                except ValueError as exc:
                    raise ValueError("capability expiry is invalid") from exc
                if requested_value.tzinfo is None:
                    requested_value = requested_value.replace(tzinfo=timezone.utc)
                else:
                    requested_value = requested_value.astimezone(timezone.utc)
                minimum_value = datetime.fromisoformat(minimum_expiry.removesuffix("Z")).replace(tzinfo=timezone.utc)
                maximum_value = datetime.fromisoformat(maximum_expiry.removesuffix("Z")).replace(tzinfo=timezone.utc)
                # A caller may provide a legacy queue deadline, including one
                # already in the past.  Repair that stale scheduling fact to
                # the active attempt's bounded lease rather than persisting a
                # capability that is born expired.  Future requests are
                # capped so this authorization can never become unbounded.
                if requested_value <= now_value:
                    capability_expiry = minimum_expiry
                elif requested_value >= maximum_value:
                    capability_expiry = maximum_expiry
                elif requested_value < minimum_value:
                    capability_expiry = minimum_expiry
                else:
                    capability_expiry = requested_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (str(dispatch_id), generation, attempt),
                )
                .fetchone()
            )
            if existing is not None:
                row = self._queue_row(existing)
                expected = (operation, schema_sha256, resolved_workspace, backend, token_sha256)
                actual = (
                    row["operation"],
                    row["schema_sha256"],
                    row["workspace_path"],
                    row["backend"],
                    row["token_sha256"],
                )
                if actual != expected:
                    raise StaleWriter("attempt capability facts conflict")
                if row["consumed_at"] is None and str(row["expires_at"]) <= now:
                    # Re-issuing an unchanged active attempt after a
                    # harness restart repairs a stale capability without
                    # changing its token or any binding fact.
                    self._db().execute(
                        "UPDATE attempt_capabilities SET expires_at = ? "
                        "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                        (capability_expiry, str(dispatch_id), generation, attempt),
                    )
                    row = self._queue_row(
                        self._db()
                        .execute(
                            "SELECT * FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                            (str(dispatch_id), generation, attempt),
                        )
                        .fetchone()
                    )
                return row
            self._db().execute(
                "INSERT INTO attempt_capabilities(dispatch_id, generation, attempt, operation, schema_sha256, workspace_path, backend, issued_at, expires_at, consumed_at, token_sha256, accepted_result_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                (
                    str(dispatch_id),
                    generation,
                    attempt,
                    operation,
                    schema_sha256,
                    resolved_workspace,
                    backend,
                    now,
                    capability_expiry,
                    token_sha256,
                ),
            )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (str(dispatch_id), generation, attempt),
                )
                .fetchone()
            )
            return self._queue_row(row)

    def submit_queue_result(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        raw_result: str | bytes,
    ) -> JsonObject:
        """Accept exactly one capability-bound raw ModelFacingResult."""

        result = self._parse_queue_result(dispatch_id, raw_result)
        raw_bytes = raw_result.encode("utf-8") if isinstance(raw_result, str) else bytes(raw_result)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        with self._transaction():
            return self._submit_queue_result_in_transaction(
                dispatch_id,
                generation=generation,
                attempt=attempt,
                token=token,
                raw_bytes=raw_bytes,
                result=result,
                digest=digest,
            )

    def _submit_queue_result_in_transaction(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        raw_bytes: bytes,
        result: object,
        digest: str,
    ) -> JsonObject:
        self._require_exact_worker_process_identity(generation, attempt)
        queue = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if queue is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        capability = (
            self._db()
            .execute(
                "SELECT * FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                (str(dispatch_id), generation, attempt),
            )
            .fetchone()
        )
        if capability is None:
            raise StaleWriter("result capability does not exist")
        if (
            int(capability["generation"]) != int(queue["generation"])
            or int(capability["attempt"]) != int(queue["attempt"])
            or str(capability["backend"]) != str(queue["backend"])
            or str(capability["workspace_path"]) != str(queue["workspace_path"])
            or str(capability["schema_sha256"]) != str(queue["result_contract_sha256"])
        ):
            raise StaleWriter("result capability binding is conflicting")
        try:
            token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise StaleWriter("result capability token is invalid") from exc
        if not secrets.compare_digest(str(capability["token_sha256"]), token_digest):
            raise StaleWriter("result capability token is invalid")
        pending_cancellation = (
            self._db()
            .execute(
                "SELECT action_id FROM recovery_controls WHERE dispatch_id = ? AND action_kind = 'cancel' "
                "ORDER BY created_at, action_id LIMIT 1",
                (str(dispatch_id),),
            )
            .fetchone()
        )
        if pending_cancellation is not None:
            raise StaleWriter("pending worker cancellation rejects a result")
        if queue["state"] == "cancelled":
            raise StaleWriter("cancelled queue dispatch rejects a result")
        if queue["state"] in {"completed", "failed"}:
            if queue["raw_result_sha256"] == digest:
                return self._queue_row(queue)
            raise StaleWriter("terminal queue result is immutable")
        if capability["consumed_at"] is not None:
            if capability["accepted_result_sha256"] == digest:
                return self._queue_row(queue)
            raise StaleWriter("result capability has already been consumed")
        if str(capability["expires_at"]) <= utc_now():
            raise StaleWriter("result capability has expired")
        late_result_transport_recovery = queue[
            "state"
        ] == "human_attention_required" and self._late_result_transport_recovery_allowed_in_transaction(
            str(dispatch_id), generation=generation, attempt=attempt
        )
        if (
            queue["state"]
            not in {
                "starting",
                "running",
                "recovery_inspection_pending",
                "recovery_retry_wait",
                "recovery_continuation_pending",
                "result_submitted",
            }
            and not late_result_transport_recovery
        ):
            raise StaleWriter(f"result submission is not allowed from queue state {queue['state']}")
        now = utc_now()
        self._db().execute(
            "UPDATE attempt_capabilities SET consumed_at = ?, accepted_result_sha256 = ? "
            "WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
            (now, digest, str(dispatch_id), generation, attempt),
        )
        self._db().execute(
            "UPDATE dispatch_queue SET state = 'result_submitted', raw_result_json = ?, "
            "raw_result_sha256 = ?, terminal_status = ?, updated_at = ? WHERE dispatch_id = ?",
            (raw_bytes.decode("utf-8"), digest, self._queue_result_terminal_status(result), now, str(dispatch_id)),
        )
        self._db().execute(
            "UPDATE recovery_state SET recovery_state = ?, next_eligible_at = NULL, "
            "human_attention_reason = NULL, updated_at = ? "
            "WHERE dispatch_id = ?",
            (
                "failed"
                if self._queue_result_terminal_status(result) in {"failed", "external_blocked", "needs_decision"}
                else "completed",
                now,
                str(dispatch_id),
            ),
        )
        row = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        return self._queue_row(row)

    @staticmethod
    def _queue_result_terminal_status(result: object) -> str:
        """Map the two worker result contracts to the shared queue status."""

        if isinstance(result, ModelFacingResult):
            return result.status.value
        from .domain import ReviewResult

        if isinstance(result, ReviewResult):
            return "completed" if result.accepted else "failed"
        raise TypeError("queue result is not a supported typed worker result")

    def _parse_queue_result(self, dispatch_id: DispatchId | str, raw_result: str | bytes) -> object:
        """Select the parser from the immutable queue result contract."""

        row = (
            self._db()
            .execute("SELECT result_contract_sha256 FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        contract = str(row["result_contract_sha256"])
        if contract == model_facing_result_schema_sha256():
            return ModelFacingResult.from_agent_message(raw_result)
        if contract == legacy_model_facing_result_schema_sha256():
            # v18 queue rows predate the additive typed blocker projection.
            # They remain parseable for migration and terminal completion, but
            # the retired digest is rejected by enqueue_dispatch above.
            return ModelFacingResult.from_agent_message(raw_result)
        if contract == model_facing_review_result_schema_sha256():
            return review_result_from_agent_message(raw_result)
        raise ValueError("queue result contract is unsupported")

    def _late_result_transport_recovery_allowed_in_transaction(
        self,
        dispatch_id: str,
        *,
        generation: int,
        attempt: int,
    ) -> bool:
        """Prove one exited attempt still owns a delayed terminal result."""

        retry = (
            self._db()
            .execute("SELECT last_failure, strategy FROM retry_policies WHERE dispatch_id = ?", (dispatch_id,))
            .fetchone()
        )
        recovery = (
            self._db()
            .execute(
                "SELECT worker_exit_classification, recovery_state FROM recovery_state WHERE dispatch_id = ?",
                (dispatch_id,),
            )
            .fetchone()
        )
        live = (
            self._db()
            .execute("SELECT generation, attempt, exited_at FROM worker_liveness WHERE dispatch_id = ?", (dispatch_id,))
            .fetchone()
        )
        return bool(
            retry is not None
            and retry["last_failure"] == RetryFailureClass.RESULT_TRANSPORT_AFTER_IDENTITY.value
            and retry["strategy"] == RecoveryStrategy.HUMAN_ATTENTION.value
            and recovery is not None
            and recovery["worker_exit_classification"] == "result-transport-after-identity"
            and recovery["recovery_state"] == "human_attention_required"
            and live is not None
            and int(live["generation"]) == generation
            and int(live["attempt"]) == attempt
            and live["exited_at"] is not None
        )

    def commit_queue_result(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        raw_result: str | bytes,
    ) -> JsonObject:
        """Atomically ingest, terminalize, release and wake one worker result."""

        result = self._parse_queue_result(dispatch_id, raw_result)
        raw_bytes = raw_result.encode("utf-8") if isinstance(raw_result, str) else bytes(raw_result)
        if len(raw_bytes) > 65_536:
            raise ValueError("worker raw result exceeds bounded limit")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        with self._transaction():
            self._submit_queue_result_in_transaction(
                dispatch_id,
                generation=generation,
                attempt=attempt,
                token=token,
                raw_bytes=raw_bytes,
                result=result,
                digest=digest,
            )
            now = utc_now()
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', next_eligible_at = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (now, str(dispatch_id)),
            )
            return self._finalize_queue_result_in_transaction(dispatch_id)

    def commit_retained_queue_result(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        raw_result: str | bytes,
    ) -> JsonObject:
        """Recover one exact private result after result transport exhausted.

        Only the unchanged, exited generation/attempt that entered typed
        result-transport attention may receive a short renewal.  The ordinary
        capability validation, cancellation fence and terminal commit remain
        the sole result authority.
        """

        result = self._parse_queue_result(dispatch_id, raw_result)
        raw_bytes = raw_result.encode("utf-8") if isinstance(raw_result, str) else bytes(raw_result)
        if len(raw_bytes) > 65_536:
            raise ValueError("worker raw result exceeds bounded limit")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        with self._transaction():
            if not self._late_result_transport_recovery_allowed_in_transaction(
                str(dispatch_id), generation=generation, attempt=attempt
            ):
                raise StaleWriter("retained result does not own an exact result-transport recovery")
            capability = (
                self._db()
                .execute(
                    "SELECT consumed_at FROM attempt_capabilities "
                    "WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (str(dispatch_id), generation, attempt),
                )
                .fetchone()
            )
            if capability is None or capability["consumed_at"] is not None:
                raise StaleWriter("retained result capability is unavailable")
            now = utc_now()
            self._db().execute(
                "UPDATE attempt_capabilities SET expires_at = ? "
                "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                (
                    self._expires_after(now, _ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS),
                    str(dispatch_id),
                    generation,
                    attempt,
                ),
            )
            self._submit_queue_result_in_transaction(
                dispatch_id,
                generation=generation,
                attempt=attempt,
                token=token,
                raw_bytes=raw_bytes,
                result=result,
                digest=digest,
            )
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', next_eligible_at = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (now, str(dispatch_id)),
            )
            return self._finalize_queue_result_in_transaction(dispatch_id)

    def ingest_recovered_result(self, dispatch_id: DispatchId | str, *, raw_result: str | bytes) -> JsonObject:
        """Ingest one strictly validated persisted-thread terminal envelope.

        This path is used only after a read-only ``thread_read`` proves the
        SDK turn already committed its terminal envelope.  It never creates a
        turn or a successor and is idempotent on the exact raw-result digest.
        """

        result = self._parse_queue_result(dispatch_id, raw_result)
        raw_bytes = raw_result.encode("utf-8") if isinstance(raw_result, str) else bytes(raw_result)
        if len(raw_bytes) > 65_536:
            raise ValueError("recovered raw result exceeds bounded limit")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if row["state"] in {"completed", "failed"}:
                if row["raw_result_sha256"] == digest:
                    return self._queue_row(row)
                raise StaleWriter("terminal queue result is immutable")
            if row["state"] != "recovery_inspection_pending":
                raise StaleWriter("persisted-thread result requires a recovery inspection state")
            now = utc_now()
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'result_submitted', raw_result_json = ?, raw_result_sha256 = ?, "
                "terminal_status = ?, updated_at = ? WHERE dispatch_id = ?",
                (raw_bytes.decode("utf-8"), digest, self._queue_result_terminal_status(result), now, str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE recovery_state SET recovery_state = ?, next_eligible_at = NULL, inspected_kind = 'terminal_result', updated_at = ? WHERE dispatch_id = ?",
                (
                    "failed"
                    if self._queue_result_terminal_status(result) in {"failed", "external_blocked", "needs_decision"}
                    else "completed",
                    now,
                    str(dispatch_id),
                ),
            )
            # A persisted terminal envelope is just as authoritative as a
            # live worker result.  Clear any pending typed retry eligibility
            # in the same transaction so restart/replay cannot reopen work.
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', next_eligible_at = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (now, str(dispatch_id)),
            )
            return self._finalize_queue_result_in_transaction(dispatch_id)

    def _ensure_controller_decision_in_transaction(
        self,
        *,
        dispatch_id: str,
        kind: str,
        cycle_sequence: int = 1,
        payload: Mapping[str, object],
        source_thread_id: str | None,
        deadline: str,
        wake_state: str,
        queue_state: str,
    ) -> str:
        """Create the v15 decision/generation pair exactly once for a wake."""

        if not isinstance(cycle_sequence, int) or isinstance(cycle_sequence, bool) or not 1 <= cycle_sequence <= 64:
            raise ValueError("controller decision cycle sequence is invalid")
        delivery_id = (
            f"wake/{dispatch_id}/{kind}" if cycle_sequence == 1 else f"wake/{dispatch_id}/{kind}/{cycle_sequence}"
        )
        decision_id = f"decision/{delivery_id}"
        existing = (
            self._db()
            .execute("SELECT decision_id FROM controller_decisions WHERE decision_id = ?", (decision_id,))
            .fetchone()
        )
        if existing is not None:
            return decision_id
        summary = {
            "dispatch_id": dispatch_id,
            "kind": kind,
            "cycle_sequence": cycle_sequence,
            "state": queue_state,
            "summary": str(payload.get("summary"))
            if isinstance(payload.get("summary"), str) and payload.get("summary")
            else f"{kind} controller decision",
            "source_thread_id": source_thread_id,
            "result_sha256": payload.get("result_sha256"),
            "expected_successor_dispatch_ids": [
                str(item[0])
                for item in self._db()
                .execute(
                    "SELECT successor_dispatch_id FROM authorized_successors WHERE source_dispatch_id = ? AND released_at IS NOT NULL ORDER BY successor_dispatch_id",
                    (dispatch_id,),
                )
                .fetchall()
            ],
        }
        summary_json = _encode_json(summary)
        now = utc_now()
        if wake_state == "pending":
            decision_state = ControllerDecisionState.PENDING_DELIVERY.value
        elif queue_state in {"completed", "failed", "cancelled"} and source_thread_id is None:
            decision_state = ControllerDecisionState.LEGACY_CLOSED.value
        else:
            decision_state = ControllerDecisionState.AWAITING_CLAIM.value
        self._db().execute(
            "INSERT INTO controller_decisions(decision_id, dispatch_id, kind, cycle_sequence, summary_json, summary_sha256, source_thread_id, state, revision, current_generation, generation_budget, generation_used, claimant_kind, claimant_id, claim_token_sha256, claim_started_at, claim_lease_expires_at, action_id, action_bundle_json, action_bundle_sha256, committed_at, acknowledged_at, superseded_at, deadline, human_attention_reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 2, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)",
            (
                decision_id,
                dispatch_id,
                kind,
                cycle_sequence,
                summary_json,
                hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
                source_thread_id,
                decision_state,
                deadline,
                now,
                now,
            ),
        )
        self._db().execute(
            "INSERT INTO controller_decision_generations(decision_id, generation, lineage_id, predecessor_generation, source_kind, state, prompt_sha256, controller_thread_id, controller_turn_id, inspection_started_at, inspection_token_sha256, inspection_lease_expires_at, inspection_completed_at, inspection_outcome, inspection_bundle_json, inspection_bundle_sha256, started_at, terminal_at, created_at, updated_at) VALUES (?, 1, ?, NULL, 'source_controller', 'prepared', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (decision_id, f"lineage/{decision_id}", now, now),
        )
        return decision_id

    def create_controller_decision(
        self,
        dispatch_id: DispatchId | str,
        *,
        kind: str,
        summary: Mapping[str, object] | None = None,
        source_thread_id: str | None = None,
        deadline: str | None = None,
    ) -> ControllerDecisionStatus:
        """Create one decision-bound wake for a queue dispatch."""

        if kind not in {"checkpoint", "terminal"}:
            raise ValueError("controller decision kind is invalid")
        dispatch = DispatchId(str(dispatch_id))
        queue = self.queue_dispatch(dispatch)
        now = utc_now()
        wake_deadline = deadline or str(queue.get("deadline") or now)
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": kind.upper(),
            "delivery_id": f"wake/{dispatch}/{kind}",
            "dispatch_id": str(dispatch),
            "state": str(queue["state"]),
            "ledger_path": os.fspath(self.path),
        }
        if summary:
            payload.update({key: value for key, value in summary.items() if key not in {"delivery_id", "dispatch_id"}})
        encoded = _encode_json(payload)
        with self._transaction():
            decision_id = self._ensure_controller_decision_in_transaction(
                dispatch_id=str(dispatch),
                kind=kind,
                payload=payload,
                source_thread_id=source_thread_id,
                deadline=wake_deadline,
                wake_state="pending" if source_thread_id else "not_applicable",
                queue_state=str(queue["state"]),
            )
            existing = self._db().execute("SELECT * FROM wake_outbox WHERE decision_id = ?", (decision_id,)).fetchone()
            if existing is None:
                self._db().execute(
                    "INSERT INTO wake_outbox(delivery_id, dispatch_id, decision_id, kind, cycle_sequence, source_thread_id, payload_json, payload_digest, state, attempt_count, source_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 0, NULL, ?, ?)",
                    (
                        f"wake/{dispatch}/{kind}",
                        str(dispatch),
                        decision_id,
                        kind,
                        source_thread_id,
                        encoded,
                        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                        "pending" if source_thread_id else "not_applicable",
                        now,
                        now,
                    ),
                )
            return self.controller_decision(decision_id)

    def finalize_queue_result(
        self,
        dispatch_id: DispatchId | str,
        *,
        successor_payload: Mapping[str, object] | None = None,
        notify_source_task: str | None = None,
    ) -> JsonObject:
        with self._transaction():
            return self._finalize_queue_result_in_transaction(
                dispatch_id,
                successor_payload=successor_payload,
                notify_source_task=notify_source_task,
            )

    def _finalize_queue_result_in_transaction(
        self,
        dispatch_id: DispatchId | str,
        *,
        successor_payload: Mapping[str, object] | None = None,
        notify_source_task: str | None = None,
    ) -> JsonObject:
        row = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        if row["state"] in {"completed", "failed", "cancelled"}:
            return self._queue_row(row)
        if row["state"] != "result_submitted":
            raise StaleWriter("queue result is not submitted")
        terminal = (
            "failed" if row["terminal_status"] in {"failed", "external_blocked", "needs_decision"} else "completed"
        )
        now = utc_now()
        authorized = (
            self._db()
            .execute(
                "SELECT successor_dispatch_id FROM authorized_successors "
                "WHERE source_dispatch_id = ? AND released_at IS NULL ORDER BY successor_dispatch_id",
                (str(dispatch_id),),
            )
            .fetchall()
        )
        authorized_ids = tuple(str(item[0]) for item in authorized)
        if successor_payload:
            requested = successor_payload.get("successor_dispatch_id")
            if requested is not None and (not isinstance(requested, str) or requested not in authorized_ids):
                raise StaleWriter("successor dispatch is not pre-authorized")
        self._db().execute(
            "UPDATE dispatch_queue SET state = ?, updated_at = ? WHERE dispatch_id = ?",
            (terminal, now, str(dispatch_id)),
        )
        for successor_id in authorized_ids:
            self._db().execute(
                "UPDATE authorized_successors SET released_at = ? WHERE source_dispatch_id = ? AND successor_dispatch_id = ? AND released_at IS NULL",
                (now, str(dispatch_id), successor_id),
            )
        successor_id = authorized_ids[0] if authorized_ids else None
        successor_payload_value = {
            "authorized_successor_dispatch_ids": list(authorized_ids),
            "released_at": now,
        }
        successor_outcome = "enqueued" if successor_id is not None else "not_required"
        self._db().execute(
            "INSERT INTO successor_outbox(source_dispatch_id, successor_dispatch_id, payload_json, outcome, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(dispatch_id),
                successor_id,
                _encode_json(successor_payload_value),
                successor_outcome,
                now,
                now,
            ),
        )
        binding = (
            self._db()
            .execute("SELECT source_thread_id FROM queue_bindings WHERE dispatch_id = ?", (str(dispatch_id),))
            .fetchone()
        )
        source_thread_id = str(binding[0]) if binding is not None and binding[0] is not None else None
        notification = {
            "schema_version": 1,
            "kind": "TERMINAL",
            "dispatch_id": str(dispatch_id),
            "run_id": str(row["run_id"]),
            "milestone_id": str(row["milestone_id"]),
            "state": terminal,
            "result_sha256": str(row["raw_result_sha256"]),
        }
        encoded = _encode_json(notification)
        self._db().execute(
            "INSERT INTO notification_outbox(dispatch_id, source_task, payload_digest, outcome, attempted_at) VALUES (?, ?, ?, 'unavailable', NULL)",
            (
                str(dispatch_id),
                notify_source_task or source_thread_id,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            ),
        )
        # Terminal closure supersedes only checkpoint decisions that never
        # committed an action.  Preserve every checkpoint wake as immutable
        # cycle/audit history: deleting only the actionless rows can leave an
        # action-bearing middle cycle behind and violate contiguous sequence
        # authority.  Suppression makes unresolved actionless deliveries
        # non-deliverable while retaining the complete ordered cycle history.
        self._db().execute(
            "UPDATE controller_decisions SET state = 'superseded', superseded_at = ?, "
            "claim_lease_expires_at = NULL, human_attention_reason = NULL, updated_at = ? "
            "WHERE dispatch_id = ? AND kind = 'checkpoint' AND action_id IS NULL "
            "AND state NOT IN ('action_committed', 'acknowledged', 'superseded', 'legacy_closed')",
            (now, now, str(dispatch_id)),
        )
        self._db().execute(
            "UPDATE wake_outbox SET state = 'suppressed', source_turn_id = NULL, updated_at = ? "
            "WHERE dispatch_id = ? AND kind = 'checkpoint' AND state != 'delivered' "
            "AND decision_id IN (SELECT decision_id FROM controller_decisions WHERE action_id IS NULL)",
            (now, str(dispatch_id)),
        )
        wake_payload = {
            "schema_version": 1,
            "kind": "TERMINAL",
            "delivery_id": f"wake/{dispatch_id}/terminal",
            "dispatch_id": str(dispatch_id),
            "state": terminal,
            "result_sha256": str(row["raw_result_sha256"]),
            "ledger_path": os.fspath(self.path),
        }
        wake_json = _encode_json(wake_payload)
        wake_state = "pending" if source_thread_id is not None else "not_applicable"
        decision_id = self._ensure_controller_decision_in_transaction(
            dispatch_id=str(dispatch_id),
            kind="terminal",
            payload=wake_payload,
            source_thread_id=source_thread_id,
            deadline=self._expires_after(now, _CONTROLLER_DECISION_WINDOW_SECONDS),
            wake_state=wake_state,
            queue_state=terminal,
        )
        self._db().execute(
            "INSERT INTO wake_outbox(delivery_id, dispatch_id, decision_id, kind, cycle_sequence, source_thread_id, payload_json, payload_digest, state, attempt_count, source_turn_id, created_at, updated_at) VALUES (?, ?, ?, 'terminal', 1, ?, ?, ?, ?, 0, NULL, ?, ?)",
            (
                f"wake/{dispatch_id}/terminal",
                str(dispatch_id),
                decision_id,
                source_thread_id,
                wake_json,
                hashlib.sha256(wake_json.encode("utf-8")).hexdigest(),
                wake_state,
                now,
                now,
            ),
        )
        row = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        return self._queue_row(row)

    def record_notification_attempt(
        self,
        dispatch_id: DispatchId | str,
        *,
        source_task: str | None,
        payload_digest: str,
        outcome: str,
    ) -> JsonObject:
        if outcome not in {"not_applicable", "unavailable", "attempted_ok", "attempted_failed"}:
            raise ValueError("notification outcome is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
            raise ValueError("notification payload digest is invalid")
        with self._transaction():
            queue = (
                self._db()
                .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if queue is None or queue["state"] not in {"completed", "failed"}:
                raise StaleWriter("terminal notification requires a terminal dispatch")
            existing = (
                self._db()
                .execute("SELECT * FROM notification_outbox WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if existing is not None:
                if str(existing["payload_digest"]) != payload_digest:
                    raise StaleWriter("terminal notification payload is conflicting")
                if (
                    existing["outcome"] == "unavailable"
                    and outcome.startswith("attempted_")
                    and existing["attempted_at"] is None
                ):
                    if existing["source_task"] is not None and existing["source_task"] != source_task:
                        raise StaleWriter("terminal notification source task is conflicting")
                    self._db().execute(
                        "UPDATE notification_outbox SET source_task = ?, outcome = ?, attempted_at = ? WHERE dispatch_id = ?",
                        (source_task, outcome, utc_now(), str(dispatch_id)),
                    )
                    updated = (
                        self._db()
                        .execute("SELECT * FROM notification_outbox WHERE dispatch_id = ?", (str(dispatch_id),))
                        .fetchone()
                    )
                    return self._queue_row(updated)
                if str(existing["source_task"] or "") != str(source_task or "") or str(existing["outcome"]) != outcome:
                    raise StaleWriter("terminal notification is already recorded")
                return self._queue_row(existing)
            attempted_at = utc_now() if outcome.startswith("attempted_") else None
            self._db().execute(
                "INSERT INTO notification_outbox(dispatch_id, source_task, payload_digest, outcome, attempted_at) VALUES (?, ?, ?, ?, ?)",
                (str(dispatch_id), source_task, payload_digest, outcome, attempted_at),
            )
            row = (
                self._db()
                .execute("SELECT * FROM notification_outbox WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            return self._queue_row(row)

    def queue_binding(self, dispatch_id: DispatchId | str) -> JsonObject:
        row = self._db().execute("SELECT * FROM queue_bindings WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise RecordNotFound(f"queue binding does not exist: {dispatch_id}")
        return self._queue_row(row)

    def rebind_legacy_active_queue(
        self,
        dispatch_id: DispatchId | str,
        *,
        source_thread_id: ThreadIdentity | str,
        permission_mode: NativePermissionMode,
        native_profile_sha256: str,
        native_compatibility_sha256: str,
        effective_permission: NativePermissionAuthority,
        checkpoint_seconds: float = 1800.0,
    ) -> JsonObject:
        """Restore authority omitted by v10 for one still-active queue row.

        The migration's read-only facts are only a placeholder. They may be
        replaced when the immutable original capsule requests the same mode as
        a complete, verified native-profile authority supplied by the caller.
        Route and binding are updated in one transaction.
        """

        dispatch = str(DispatchId(dispatch_id))
        source = (
            source_thread_id.id
            if isinstance(source_thread_id, ThreadIdentity)
            else ThreadIdentity(str(source_thread_id)).id
        )
        if not isinstance(permission_mode, NativePermissionMode):
            permission_mode = NativePermissionMode(permission_mode)
        _sha256(native_profile_sha256, field_name="legacy queue native profile digest")
        _sha256(native_compatibility_sha256, field_name="legacy queue native compatibility digest")
        if effective_permission.mode is not permission_mode:
            raise NativePermissionConflict("legacy queue permission mode conflicts with verified authority")
        permission_json = _encode_json(effective_permission.facts)
        permission_sha256 = hashlib.sha256(permission_json.encode("utf-8")).hexdigest()
        now = utc_now()
        checkpoint_deadline = self._expires_after(now, checkpoint_seconds)
        with self._transaction():
            queue = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (dispatch,)).fetchone()
            binding = self._db().execute("SELECT * FROM queue_bindings WHERE dispatch_id = ?", (dispatch,)).fetchone()
            if queue is None or binding is None:
                raise RecordNotFound(f"legacy queue dispatch does not exist: {dispatch}")
            if queue["state"] not in {"queued", "claimed", "starting", "running"}:
                raise StaleWriter("only an active legacy queue dispatch may be rebound")
            if binding["plan_path"] != "legacy-v10":
                raise StaleWriter("queue dispatch is not a migrated v10 recovery candidate")
            capsule = _decode_json_object(str(queue["capsule_json"]), field_name="legacy queue capsule")
            if capsule is None or capsule.get("permission_mode") != permission_mode.value:
                raise NativePermissionConflict(
                    "original queue capsule does not authorize the recovered permission mode"
                )
            route = _decode_json_object(str(queue["route_json"]), field_name="legacy queue route")
            if route is None:
                raise CorruptSchemaError("legacy queue route is missing")
            recovered_route = dict(route)
            recovered_facts = {
                "effective_permission": effective_permission.facts,
                "native_profile_sha256": native_profile_sha256,
            }
            for key, value in recovered_facts.items():
                if key in recovered_route and recovered_route[key] != value:
                    raise NativePermissionConflict("legacy queue route conflicts with verified native authority")
                recovered_route[key] = value
            prior_route_compatibility = recovered_route.get("native_compatibility_sha256")
            if prior_route_compatibility is not None:
                _sha256(str(prior_route_compatibility), field_name="legacy route native compatibility digest")
            recovered_route["native_compatibility_sha256"] = native_compatibility_sha256
            route_json = _encode_json(recovered_route)
            current_is_exact = (
                binding["source_thread_id"] == source
                and binding["permission_mode"] == permission_mode.value
                and binding["native_profile_sha256"] == native_profile_sha256
                and binding["native_compatibility_sha256"] == native_compatibility_sha256
                and binding["effective_permission_json"] == permission_json
                and binding["effective_permission_sha256"] == permission_sha256
                and queue["route_json"] == route_json
            )
            if not current_is_exact:
                legacy_placeholder = binding["source_thread_id"] is None and (
                    binding["permission_mode"] == NativePermissionMode.READ_ONLY.value
                    and binding["native_profile_sha256"] is None
                    and binding["native_compatibility_sha256"] is None
                )
                malformed_source_only = (
                    binding["source_thread_id"] == repr(ThreadIdentity(source))
                    and binding["permission_mode"] == permission_mode.value
                    and binding["native_profile_sha256"] == native_profile_sha256
                    and binding["native_compatibility_sha256"] == native_compatibility_sha256
                    and binding["effective_permission_json"] == permission_json
                    and binding["effective_permission_sha256"] == permission_sha256
                    and queue["route_json"] == route_json
                )
                # The H6-E-W worker digest removes only recursive ambient
                # discovery identities. Exact profile, permission, source and
                # route facts prove that an otherwise-different broad digest
                # can be replaced without broadening worker authority.
                scoped_compatibility_upgrade = (
                    binding["source_thread_id"] == source
                    and binding["permission_mode"] == permission_mode.value
                    and binding["native_profile_sha256"] == native_profile_sha256
                    and binding["native_compatibility_sha256"] == prior_route_compatibility
                    and binding["effective_permission_json"] == permission_json
                    and binding["effective_permission_sha256"] == permission_sha256
                )
                if not legacy_placeholder and not malformed_source_only and not scoped_compatibility_upgrade:
                    raise NativePermissionConflict("legacy queue already carries conflicting recovery authority")
                self._db().execute(
                    "UPDATE dispatch_queue SET route_json = ?, updated_at = ? WHERE dispatch_id = ?",
                    (route_json, now, dispatch),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET source_thread_id = ?, permission_mode = ?, "
                    "native_profile_sha256 = ?, native_compatibility_sha256 = ?, "
                    "effective_permission_json = ?, effective_permission_sha256 = ?, "
                    "checkpoint_deadline = ?, checkpoint_armed = 1, updated_at = ? WHERE dispatch_id = ?",
                    (
                        source,
                        permission_mode.value,
                        native_profile_sha256,
                        native_compatibility_sha256,
                        permission_json,
                        permission_sha256,
                        checkpoint_deadline,
                        now,
                        dispatch,
                    ),
                )
            updated = self._db().execute("SELECT * FROM queue_bindings WHERE dispatch_id = ?", (dispatch,)).fetchone()
            if updated is None:  # pragma: no cover - transaction owns the row
                raise LedgerError("legacy queue rebind removed its binding")
            return self._queue_row(updated)

    def next_checkpoint_deadline(self) -> str | None:
        """Return the next armed checkpoint without changing ledger state."""

        row = (
            self._db()
            .execute(
                "SELECT MIN(b.checkpoint_deadline) FROM queue_bindings b JOIN dispatch_queue q "
                "ON q.dispatch_id = b.dispatch_id WHERE b.checkpoint_armed = 1 "
                "AND q.state IN ('claimed','starting','running','result_submitted','human_attention_required')"
            )
            .fetchone()
        )
        value = row[0] if row is not None else None
        return str(value) if value is not None else None

    def authorize_successor(
        self,
        source_dispatch_id: DispatchId | str,
        successor_dispatch_id: DispatchId | str,
    ) -> JsonObject:
        """Record one successor before source execution; workers cannot add one."""

        source = str(source_dispatch_id)
        successor = str(successor_dispatch_id)
        if not source or not successor or source == successor:
            raise ValueError("successor identity is invalid")
        with self._transaction():
            source_row = (
                self._db().execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (source,)).fetchone()
            )
            successor_row = (
                self._db().execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (successor,)).fetchone()
            )
            if source_row is None or successor_row is None:
                raise RecordNotFound("successor authorization references an unknown queue dispatch")
            if source_row["state"] in {"result_submitted", "finalizing", "completed", "failed", "cancelled"}:
                raise StaleWriter("successor authorization must precede source execution")
            if successor_row["state"] != "queued":
                raise StaleWriter("successor authorization requires a queued dispatch")
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM authorized_successors WHERE source_dispatch_id = ? AND successor_dispatch_id = ?",
                    (source, successor),
                )
                .fetchone()
            )
            if existing is not None:
                return self._queue_row(existing)
            self._db().execute(
                "INSERT INTO authorized_successors(source_dispatch_id, successor_dispatch_id, authorized_at, released_at) VALUES (?, ?, ?, NULL)",
                (source, successor, utc_now()),
            )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM authorized_successors WHERE source_dispatch_id = ? AND successor_dispatch_id = ?",
                    (source, successor),
                )
                .fetchone()
            )
            return self._queue_row(row)

    def authorized_successors(self, source_dispatch_id: DispatchId | str) -> tuple[JsonObject, ...]:
        rows = (
            self._db()
            .execute(
                "SELECT * FROM authorized_successors WHERE source_dispatch_id = ? ORDER BY successor_dispatch_id",
                (str(source_dispatch_id),),
            )
            .fetchall()
        )
        return tuple(self._queue_row(row) for row in rows)

    @staticmethod
    def _expires_after(now: str, seconds: float) -> str:
        if seconds <= 0:
            raise ValueError("lease/checkpoint duration must be positive")
        timestamp = datetime.fromisoformat(now.removesuffix("Z")).replace(tzinfo=timezone.utc).timestamp() + seconds
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _require_exact_worker_process_identity(generation: int, attempt: int) -> None:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise ValueError("worker process identity is invalid")

    def bind_worker_liveness(
        self,
        dispatch_id: DispatchId | str,
        *,
        epoch: int,
        generation: int,
        attempt: int,
        pid: int,
        process_birth_identity: str,
        lease_token_sha256: str,
        lease_seconds: float = 30.0,
    ) -> JsonObject:
        self._require_exact_worker_process_identity(generation, attempt)
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not process_birth_identity
            or not re.fullmatch(r"[0-9a-f]{64}", lease_token_sha256)
        ):
            raise ValueError("worker liveness facts are invalid")
        now = utc_now()
        expires = self._expires_after(now, lease_seconds)
        with self._transaction():
            self._require_live_harness_in_transaction(epoch)
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if (int(queue["generation"]), int(queue["attempt"]), queue["claim_epoch"]) != (
                generation,
                attempt,
                epoch,
            ):
                raise StaleWriter("worker liveness claim is stale")
            capability = (
                self._db()
                .execute(
                    "SELECT token_sha256 FROM attempt_capabilities "
                    "WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (str(dispatch_id), generation, attempt),
                )
                .fetchone()
            )
            if capability is None or str(capability["token_sha256"]) != lease_token_sha256:
                raise StaleWriter("worker liveness token is not bound to the attempt capability")
            if queue["state"] not in {"claimed", "starting", "running"}:
                raise StaleWriter("worker liveness requires an active queue claim")
            capability_lifetime = min(
                max(float(lease_seconds), _ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS),
                _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS,
            )
            self._db().execute(
                "UPDATE attempt_capabilities SET expires_at = ? "
                "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                (self._expires_after(now, capability_lifetime), str(dispatch_id), generation, attempt),
            )
            existing = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if existing is not None:
                if (
                    int(existing["generation"]),
                    int(existing["attempt"]),
                    int(existing["pid"]),
                    str(existing["process_birth_identity"]),
                    str(existing["lease_token_sha256"]),
                ) != (generation, attempt, pid, process_birth_identity, lease_token_sha256):
                    raise StaleWriter("worker liveness identity is conflicting")
            else:
                self._db().execute(
                    "INSERT INTO worker_liveness(dispatch_id, generation, attempt, pid, process_birth_identity, lease_token_sha256, lease_expires_at, last_seen_at, exited_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        str(dispatch_id),
                        generation,
                        attempt,
                        pid,
                        process_birth_identity,
                        lease_token_sha256,
                        expires,
                        now,
                    ),
                )
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'starting', updated_at = ? WHERE dispatch_id = ? AND state = 'claimed'",
                (now, str(dispatch_id)),
            )
            row = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            return self._queue_row(row)

    def bind_worker_thread(
        self,
        dispatch_id: DispatchId | str,
        *,
        epoch: int,
        generation: int,
        attempt: int,
        token: str,
        thread_id: str,
    ) -> JsonObject:
        self._require_exact_worker_process_identity(generation, attempt)
        try:
            ThreadIdentity(thread_id)
        except ValueError as exc:
            raise ValueError("worker thread identity is invalid") from exc
        try:
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise StaleWriter("worker capability token is invalid") from exc
        with self._transaction():
            self._require_live_harness_in_transaction(epoch)
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if (int(queue["generation"]), int(queue["attempt"]), queue["claim_epoch"]) != (
                generation,
                attempt,
                epoch,
            ):
                raise StaleWriter("worker thread claim is stale")
            capability = (
                self._db()
                .execute(
                    "SELECT token_sha256 FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                    (str(dispatch_id), generation, attempt),
                )
                .fetchone()
            )
            if capability is None or str(capability[0]) != token_hash:
                raise StaleWriter("worker capability does not match thread binding")
            if queue["thread_id"] is not None and str(queue["thread_id"]) != thread_id:
                raise StaleWriter("worker thread identity is conflicting")
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'running', thread_id = ?, updated_at = ? "
                "WHERE dispatch_id = ? AND state IN ('claimed','starting','running')",
                (thread_id, utc_now(), str(dispatch_id)),
            )
            row = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            return self._queue_row(row)

    def worker_attempt_capability(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
    ) -> JsonObject:
        """Look up one exact worker capability without renewing its lease."""

        self._require_exact_worker_process_identity(generation, attempt)
        try:
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise StaleWriter("worker capability token is invalid") from exc
        queue = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if queue is None:
            raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
        if (int(queue["generation"]), int(queue["attempt"])) != (generation, attempt):
            raise StaleWriter("worker capability process identity is stale")
        capability = (
            self._db()
            .execute(
                "SELECT * FROM attempt_capabilities WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                (str(dispatch_id), generation, attempt),
            )
            .fetchone()
        )
        if capability is None or not secrets.compare_digest(str(capability["token_sha256"]), token_hash):
            raise StaleWriter("worker capability token is invalid")
        return self._queue_row(capability)

    def renew_worker_liveness(
        self,
        dispatch_id: DispatchId | str,
        *,
        generation: int,
        attempt: int,
        token: str,
        lease_seconds: float = 30.0,
        epoch: int | None = None,
    ) -> JsonObject:
        self._require_exact_worker_process_identity(generation, attempt)
        try:
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        except (AttributeError, UnicodeEncodeError) as exc:
            raise StaleWriter("worker lease token is invalid") from exc
        now = utc_now()
        with self._transaction():
            if epoch is not None:
                self._require_live_harness_in_transaction(epoch)
            row = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if row is None:
                raise StaleWriter("worker liveness lease does not exist")
            if (int(row["generation"]), int(row["attempt"]), str(row["lease_token_sha256"])) != (
                generation,
                attempt,
                token_hash,
            ):
                raise StaleWriter("worker liveness lease is stale")
            queue = (
                self._db()
                .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if queue is None or queue["state"] not in {"starting", "running"}:
                raise StaleWriter("worker liveness lease is no longer active")
            expires = self._expires_after(now, lease_seconds)
            self._db().execute(
                "UPDATE worker_liveness SET lease_expires_at = ?, last_seen_at = ? WHERE dispatch_id = ?",
                (expires, now, str(dispatch_id)),
            )
            capability_lifetime = min(
                max(float(lease_seconds), _ATTEMPT_CAPABILITY_MIN_LIFETIME_SECONDS),
                _ATTEMPT_CAPABILITY_MAX_LIFETIME_SECONDS,
            )
            self._db().execute(
                "UPDATE attempt_capabilities SET expires_at = ? "
                "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                (self._expires_after(now, capability_lifetime), str(dispatch_id), generation, attempt),
            )
            updated = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if updated is not None:
                return self._queue_row(updated)
            queue_after = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue_after is None:  # pragma: no cover - foreign-key ownership
                raise LedgerError("worker exit removed its queue dispatch")
            return self._queue_row(queue_after)

    def worker_liveness(self, dispatch_id: DispatchId | str) -> JsonObject | None:
        row = self._db().execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        return self._queue_row(row) if row is not None else None

    def adopt_live_worker(
        self,
        dispatch_id: DispatchId | str,
        *,
        new_epoch: int,
        pid: int,
        process_birth_identity: str,
        claim_nonce_sha256: str,
    ) -> JsonObject:
        """Move a still-live worker claim to a replacement harness epoch."""

        if new_epoch <= 0 or pid <= 0 or not process_birth_identity:
            raise ValueError("live worker adoption facts are invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", claim_nonce_sha256):
            raise ValueError("worker adoption claim nonce is invalid")
        with self._transaction():
            self._require_live_harness_in_transaction(new_epoch)
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            live = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if queue is None or live is None:
                raise RecordNotFound(f"live worker does not exist: {dispatch_id}")
            if (
                int(live["pid"]) != pid
                or str(live["process_birth_identity"]) != process_birth_identity
                or live["exited_at"] is not None
            ):
                raise StaleWriter("worker adoption identity is stale")
            if queue["state"] not in {"claimed", "starting", "running"}:
                raise StaleWriter("only an active worker may be adopted")
            self._db().execute(
                "UPDATE dispatch_queue SET claim_epoch = ?, claim_nonce_sha256 = ?, updated_at = ? "
                "WHERE dispatch_id = ? AND state IN ('claimed','starting','running')",
                (new_epoch, claim_nonce_sha256, utc_now(), str(dispatch_id)),
            )
            updated = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            return self._queue_row(updated)

    def mark_worker_exit(
        self,
        dispatch_id: DispatchId | str,
        *,
        pid: int,
        process_birth_identity: str,
        exit_code: int | None = None,
        classification: str | None = None,
    ) -> JsonObject:
        """Atomically close worker liveness and arm one recovery inspection."""

        if classification is not None and (
            not isinstance(classification, str) or not classification or len(classification) > 128
        ):
            raise ValueError("worker-exit classification is invalid")
        now = utc_now()
        with self._transaction():
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            row = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if row is None or queue is None:
                raise RecordNotFound(f"worker liveness does not exist: {dispatch_id}")
            if int(row["pid"]) != pid or str(row["process_birth_identity"]) != process_birth_identity:
                raise StaleWriter("worker exit identity is conflicting")
            if row["exited_at"] is not None:
                return self._queue_row(queue)
            # A result may have crossed the IPC boundary just before the
            # worker process exits.  Terminal/result-submitted queue state is
            # already authoritative; retain the exit fact without reopening
            # recovery or changing the immutable result.
            if queue["state"] in {"result_submitted", "completed", "failed", "cancelled"}:
                self._db().execute(
                    "UPDATE worker_liveness SET exited_at = ?, last_seen_at = ? WHERE dispatch_id = ? AND exited_at IS NULL",
                    (now, now, str(dispatch_id)),
                )
                # A terminal result crossed the boundary before this process
                # exit was observed.  Commands that never reached the worker,
                # or whose acknowledgement was lost, are explicitly
                # unresolved and must never be replayed against a new turn.
                self._db().execute(
                    "UPDATE control_commands SET state = 'unresolved', acknowledged_at = COALESCE(acknowledged_at, ?) "
                    "WHERE dispatch_id = ? AND state IN ('pending', 'sent')",
                    (now, str(dispatch_id)),
                )
                return self._queue_row(queue)
            self._db().execute(
                "UPDATE worker_liveness SET exited_at = ?, last_seen_at = ? WHERE dispatch_id = ? AND exited_at IS NULL",
                (now, now, str(dispatch_id)),
            )
            recovery = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if recovery is None:
                raise CorruptSchemaError("worker exit lacks v12 recovery authority")
            inspections = int(recovery["inspection_used"])
            budget = int(recovery["inspection_budget"])
            provider_transient_exit = classification == "transient-after-identity" or exit_code == 79
            next_state = "recovery_inspection_pending" if inspections < budget else "human_attention_required"
            reason = None if next_state != "human_attention_required" else "inspection budget exhausted at worker exit"
            self._db().execute(
                "UPDATE recovery_state SET worker_exit_classification = ?, exit_code = ?, recovery_state = ?, "
                "inspection_used = ?, next_eligible_at = NULL, human_attention_reason = ?, exited_at = ?, updated_at = ? "
                "WHERE dispatch_id = ?",
                (
                    classification or (f"exit-{exit_code}" if exit_code is not None else "worker-exit"),
                    exit_code,
                    next_state,
                    # Provider failures are already terminally typed by the
                    # worker. Count the subsequent persisted-thread read as
                    # the inspection, so four reads support three automatic
                    # continuations plus one final failed turn.
                    inspections + (0 if provider_transient_exit else (1 if inspections < budget else 0)),
                    reason,
                    now,
                    now,
                    str(dispatch_id),
                ),
            )
            self._db().execute(
                "UPDATE dispatch_queue SET state = ?, updated_at = ? WHERE dispatch_id = ? "
                "AND state IN ('claimed','starting','running')",
                (next_state, now, str(dispatch_id)),
            )
            if next_state == "human_attention_required":
                self._db().execute(
                    "UPDATE retry_policies SET revision = revision + 1, strategy = 'human_attention_required', "
                    "next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    (reason, now, str(dispatch_id)),
                )
            # Exit classification and retry consumption are one transaction.
            # The typed class is derived from the closed worker exit code;
            # unknown/non-transient exits consume no automatic budget and are
            # recorded as fail-closed human attention.
            retry_by_classification = {
                "transport-before-identity": RetryFailureClass.PRE_IDENTITY_TRANSPORT,
                "response-chain-invalid": RetryFailureClass.INVALID_RESPONSE_CHAIN,
                "schema-output-invalid": RetryFailureClass.SCHEMA_ENVELOPE,
                "transient-after-identity": RetryFailureClass.PROVIDER_TRANSIENT,
                "authentication-failure": RetryFailureClass.AUTHENTICATION,
                "permission-failure": RetryFailureClass.PERMISSION,
                "capability-failure": RetryFailureClass.CAPABILITY,
                "profile-failure": RetryFailureClass.PROFILE,
                "integrity-failure": RetryFailureClass.INTEGRITY,
                "malformed-input": RetryFailureClass.MALFORMED_INPUT,
                "result-transport-after-identity": RetryFailureClass.RESULT_TRANSPORT_AFTER_IDENTITY,
                "unknown-sdk-failure": RetryFailureClass.UNKNOWN,
            }
            retry_failure = retry_by_classification.get(str(classification))
            if retry_failure is None:
                retry_failure = retry_by_classification.get(
                    {
                        74: "transport-before-identity",
                        75: "response-chain-invalid",
                        77: "schema-output-invalid",
                        79: "transient-after-identity",
                        80: "authentication-failure",
                        81: "permission-failure",
                        82: "capability-failure",
                        83: "profile-failure",
                        84: "integrity-failure",
                        85: "malformed-input",
                        88: "result-transport-after-identity",
                        86: "unknown-sdk-failure",
                        87: "unknown-sdk-failure",
                    }.get(exit_code, "")
                )
            self._apply_exit_retry_policy_in_transaction(
                dispatch_id,
                queue_state=str(queue["state"]),
                failure=retry_failure or RetryFailureClass.UNKNOWN,
                prior_thread_id=str(queue["thread_id"]) if queue["thread_id"] else None,
                worker_was_bound=True,
                allow_ambiguous_inspection=(
                    classification in {None, "worker-exit", "restart-observed-worker-dead"} and retry_failure is None
                ),
            )
            self._fault("after_worker_exit")
            updated = (
                self._db()
                .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if updated is not None:
                return self._queue_row(updated)
            queue_after = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue_after is None:  # pragma: no cover - foreign-key ownership
                raise LedgerError("worker exit removed its queue dispatch")
            return self._queue_row(queue_after)

    def _apply_exit_retry_policy_in_transaction(
        self,
        dispatch_id: DispatchId | str,
        *,
        queue_state: str,
        failure: RetryFailureClass,
        prior_thread_id: str | None,
        worker_was_bound: bool,
        allow_ambiguous_inspection: bool = False,
    ) -> None:
        """Consume one closed retry class while the exit transaction is open."""

        row = self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise CorruptSchemaError("worker exit lacks retry-policy authority")
        now = utc_now()
        retry_columns = {
            RetryFailureClass.PRE_IDENTITY_TRANSPORT: (
                "pre_identity_used",
                "pre_identity_budget",
                RecoveryStrategy.BACKOFF,
            ),
            RetryFailureClass.INVALID_RESPONSE_CHAIN: (
                "invalid_chain_used",
                "invalid_chain_budget",
                RecoveryStrategy.FRESH_THREAD,
            ),
            RetryFailureClass.SCHEMA_ENVELOPE: (
                "schema_envelope_used",
                "schema_envelope_budget",
                RecoveryStrategy.SAME_THREAD_SCHEMA_CORRECTION,
            ),
            RetryFailureClass.POST_IDENTITY_LOSS: (
                "post_identity_loss_used",
                "post_identity_loss_budget",
                RecoveryStrategy.SAME_THREAD_CONTINUATION,
            ),
            RetryFailureClass.PROVIDER_TRANSIENT: (
                "provider_transient_used",
                "provider_transient_budget",
                RecoveryStrategy.SAME_THREAD_CONTINUATION,
            ),
        }
        selected = retry_columns.get(failure)
        if selected is not None and int(row[selected[0]]) < int(row[selected[1]]):
            used_column, budget_column, strategy = selected
            used = int(row[used_column]) + 1
            eligible = None
            if strategy in {RecoveryStrategy.BACKOFF, RecoveryStrategy.SAME_THREAD_CONTINUATION}:
                jitter = int(hashlib.sha256(f"{dispatch_id}:{used}".encode()).hexdigest()[:4], 16) / 65535
                eligible = self._expires_after(now, min(300.0, (2 ** (used - 1)) + jitter))
            self._db().execute(
                f"UPDATE retry_policies SET {used_column} = ?, revision = revision + 1, last_failure = ?, strategy = ?, next_eligible_at = ?, prior_thread_id = ?, human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (used, failure.value, strategy.value, eligible, prior_thread_id, now, str(dispatch_id)),
            )
            if failure is RetryFailureClass.INVALID_RESPONSE_CHAIN:
                self._db().execute(
                    "UPDATE recovery_state SET fresh_thread_after_idle = 1, fresh_thread_budget = MAX(fresh_thread_budget, 1), updated_at = ? WHERE dispatch_id = ? AND fresh_thread_used = 0",
                    (now, str(dispatch_id)),
                )
            if failure is RetryFailureClass.PROVIDER_TRANSIENT:
                self._db().execute(
                    "UPDATE recovery_state SET continuation_budget = MAX(continuation_budget, ?), updated_at = ? "
                    "WHERE dispatch_id = ?",
                    (int(row[budget_column]), now, str(dispatch_id)),
                )
            if failure is RetryFailureClass.PRE_IDENTITY_TRANSPORT:
                # The process is already durably exited, so a pre-identity
                # transport loss can safely return to FIFO queueing in this
                # same transaction.
                self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(dispatch_id),))
                self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, inspected_thread_id = NULL, inspected_turn_id = NULL, inspected_kind = NULL, human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                    (now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, claim_nonce_sha256 = NULL, attempt = attempt + 1, thread_id = NULL, host_id = NULL, available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                    (eligible or now, now, str(dispatch_id)),
                )
            elif queue_state == "queued" and strategy is RecoveryStrategy.BACKOFF:
                self._db().execute(
                    "UPDATE dispatch_queue SET available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                    (eligible or now, now, str(dispatch_id)),
                )
            return
        reason = (
            "native compatibility profile changed before SDK identity"
            if failure is RetryFailureClass.PROFILE
            else "result submission transport recovery exhausted after worker identity"
            if failure is RetryFailureClass.RESULT_TRANSPORT_AFTER_IDENTITY
            else f"automatic retry is not authorized for {failure.value}"
        )
        if (
            allow_ambiguous_inspection
            and failure is RetryFailureClass.UNKNOWN
            and worker_was_bound
            and queue_state
            in {
                "claimed",
                "starting",
                "running",
                "recovery_inspection_pending",
                "recovery_retry_wait",
                "recovery_continuation_pending",
            }
        ):
            # A generic process exit has no typed SDK classification.  Keep
            # the durable read-only inspection boundary, but consume no retry
            # budget; only an explicit terminal inspection may authorize any
            # subsequent recovery decision.
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, last_failure = ?, strategy = 'none', next_eligible_at = NULL, human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (failure.value, now, str(dispatch_id)),
            )
            return
        # Explicit unknown and malformed SDK outcomes are terminal fail-closed
        # classifications.  They must not enter read-only inspection, because
        # an idle-looking persisted thread could otherwise be turned into an
        # automatic continuation despite consuming zero retry budget.
        self._db().execute(
            "UPDATE retry_policies SET revision = revision + 1, last_failure = ?, strategy = 'human_attention_required', next_eligible_at = NULL, prior_thread_id = ?, prior_turn_id = NULL, human_attention_reason = ?, updated_at = ? WHERE dispatch_id = ?",
            (failure.value, prior_thread_id, reason, now, str(dispatch_id)),
        )
        self._db().execute(
            "UPDATE recovery_state SET recovery_state = 'human_attention_required', next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? WHERE dispatch_id = ?",
            (reason, now, str(dispatch_id)),
        )
        self._db().execute(
            "UPDATE dispatch_queue SET state = 'human_attention_required', updated_at = ? WHERE dispatch_id = ? AND state NOT IN ('completed','failed','cancelled','result_submitted','finalizing')",
            (now, str(dispatch_id)),
        )

    def recovery_state(self, dispatch_id: DispatchId | str) -> JsonObject:
        row = self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
        return self._queue_row(row)

    def configure_recovery(
        self,
        dispatch_id: DispatchId | str,
        *,
        provider_env_key: str,
        profile_sha256: str,
        inspection_budget: int = 4,
        continuation_budget: int = 1,
        fresh_thread_after_idle: bool = False,
        fresh_thread_budget: int = 0,
    ) -> JsonObject:
        """Bind secretless profile identity and immutable recovery budgets."""

        if re.fullmatch(r"[A-Z][A-Z0-9_]*", provider_env_key) is None:
            raise ValueError("provider environment key name is invalid")
        _sha256(profile_sha256, field_name="provider profile digest")
        if (
            isinstance(inspection_budget, bool)
            or not isinstance(inspection_budget, int)
            or not 0 <= inspection_budget <= 16
            or isinstance(continuation_budget, bool)
            or not isinstance(continuation_budget, int)
            or not 0 <= continuation_budget <= 8
            or not isinstance(fresh_thread_after_idle, bool)
            or isinstance(fresh_thread_budget, bool)
            or not isinstance(fresh_thread_budget, int)
            or not 0 <= fresh_thread_budget <= 4
            or (not fresh_thread_after_idle and fresh_thread_budget != 0)
        ):
            raise ValueError("recovery policy budgets are invalid")
        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
            if row["provider_env_key"] is not None and (
                row["provider_env_key"] != provider_env_key or row["profile_sha256"] != profile_sha256
            ):
                raise StaleWriter("recovery profile identity is immutable")
            if int(row["inspection_used"]) > inspection_budget or int(row["continuation_used"]) > continuation_budget:
                raise StaleWriter("recovery policy would reset consumed budget")
            if int(row["fresh_thread_used"]) > fresh_thread_budget:
                raise StaleWriter("recovery policy would reset fresh-thread budget")
            self._db().execute(
                "UPDATE recovery_state SET provider_env_key = ?, profile_sha256 = ?, inspection_budget = ?, "
                "continuation_budget = ?, fresh_thread_after_idle = ?, fresh_thread_budget = ?, updated_at = ? "
                "WHERE dispatch_id = ?",
                (
                    provider_env_key,
                    profile_sha256,
                    inspection_budget,
                    continuation_budget,
                    int(fresh_thread_after_idle),
                    fresh_thread_budget,
                    utc_now(),
                    str(dispatch_id),
                ),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def record_recovery_inspection(
        self,
        dispatch_id: DispatchId | str,
        *,
        kind: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        next_state: str | None = None,
        next_eligible_at: str | None = None,
        attention_reason: str | None = None,
    ) -> JsonObject:
        """Commit one bounded read-only inspection decision."""

        # A terminal result carries a complete ModelFacingResult payload and
        # must enter through ``ingest_recovered_result``.  Allowing a bare
        # terminal inspection here would mark the queue terminal without its
        # immutable result/digest authority.
        if kind == "terminal_result":
            raise ValueError("terminal result inspection requires ingest_recovered_result")
        allowed = {
            "active_writer",
            "idle_no_result",
            "transient_failed_turn",
            "invalid_chain_empty_history",
            "invalid_chain_failed_turn",
            "ambiguous",
            "malformed",
        }
        if kind not in allowed:
            raise ValueError("recovery inspection kind is unsupported")
        if next_state is None:
            next_state = {
                "active_writer": "recovery_retry_wait",
                "idle_no_result": "recovery_continuation_pending",
                "transient_failed_turn": "recovery_continuation_pending",
                "invalid_chain_empty_history": "recovery_continuation_pending",
                "invalid_chain_failed_turn": "recovery_continuation_pending",
                "ambiguous": "human_attention_required",
                "malformed": "human_attention_required",
            }[kind]
        if next_state not in {
            "recovery_retry_wait",
            "recovery_continuation_pending",
            "human_attention_required",
        }:
            raise ValueError("recovery decision state is unsupported")
        if next_state in {"recovery_retry_wait", "recovery_continuation_pending"} and not next_eligible_at:
            raise ValueError("bounded recovery state requires eligibility")
        if attention_reason is not None and (
            not isinstance(attention_reason, str) or not attention_reason.strip() or len(attention_reason) > 512
        ):
            raise ValueError("human-attention reason is invalid")
        if attention_reason is not None:
            attention_reason = redact_control_text(attention_reason, limit=512)
        with self._transaction():
            row = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if row is None:
                raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
            current_state = str(row["recovery_state"])
            if current_state in {"human_attention_required", "completed", "failed"}:
                return self._queue_row(row)
            if current_state not in {
                "recovery_inspection_pending",
                "recovery_retry_wait",
                "recovery_continuation_pending",
            }:
                raise StaleWriter("recovery inspection is not pending")
            used = int(row["inspection_used"])
            budget = int(row["inspection_budget"])
            if used >= budget and kind != "terminal_result":
                next_state = "human_attention_required"
                next_eligible_at = None
            reason = (
                None
                if next_state != "human_attention_required"
                else (attention_reason or f"recovery inspection: {kind}")
            )
            self._db().execute(
                "UPDATE recovery_state SET recovery_state = ?, inspection_used = ?, next_eligible_at = ?, inspected_thread_id = ?, "
                "inspected_turn_id = ?, inspected_kind = ?, human_attention_reason = ?, updated_at = ? WHERE dispatch_id = ?",
                (
                    next_state,
                    min(used + 1, budget),
                    next_eligible_at,
                    thread_id,
                    turn_id,
                    kind,
                    reason,
                    utc_now(),
                    str(dispatch_id),
                ),
            )
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, updated_at = ? WHERE dispatch_id = ?",
                (utc_now(), str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE dispatch_queue SET state = ?, updated_at = ? WHERE dispatch_id = ? "
                "AND state IN ('recovery_inspection_pending','recovery_retry_wait','recovery_continuation_pending')",
                (next_state, utc_now(), str(dispatch_id)),
            )
            if next_state == "human_attention_required":
                self._db().execute(
                    "UPDATE retry_policies SET strategy = 'human_attention_required', "
                    "next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    (reason, utc_now(), str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? "
                    "WHERE dispatch_id = ? AND checkpoint_armed = 1",
                    (utc_now(), str(dispatch_id)),
                )
            self._fault("after_recovery_inspection")
            return self._queue_row(
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def mark_human_attention_required(
        self,
        dispatch_id: DispatchId | str,
        *,
        reason: str,
        failure_class: RetryFailureClass | str | None = None,
    ) -> JsonObject:
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 512
            or len(reason.encode("utf-8")) > 512
            or "\x00" in reason
        ):
            raise ValueError("human-attention reason is invalid")
        reason = redact_control_text(reason, limit=512)
        failure = None if failure_class is None else RetryFailureClass(failure_class).value
        # An active queue with no liveness/ownership identity is ambiguous
        # before an inspection can begin.  Close it exactly once with a
        # sanitized human-attention fact rather than blindly requeueing it.
        with self._transaction():
            recovery = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if recovery is None or queue is None:
                raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
            current = str(recovery["recovery_state"])
            if current == "human_attention_required" or current in {"completed", "failed"}:
                return self._queue_row(queue)
            if current == "none":
                if str(queue["state"]) not in {"queued", "claimed", "starting", "running"}:
                    raise StaleWriter("human-attention recovery is not pending")
                now = utc_now()
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'human_attention_required', human_attention_reason = ?, "
                    "next_eligible_at = NULL, inspected_thread_id = NULL, inspected_turn_id = NULL, "
                    "inspected_kind = NULL, updated_at = ? WHERE dispatch_id = ?",
                    (reason, now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE retry_policies SET revision = revision + 1, last_failure = ?, "
                    "strategy = 'human_attention_required', "
                    "next_eligible_at = NULL, prior_thread_id = NULL, prior_turn_id = NULL, "
                    "human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    (failure, reason, now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'human_attention_required', updated_at = ? WHERE dispatch_id = ? AND state IN ('queued','claimed','starting','running')",
                    (now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? "
                    "WHERE dispatch_id = ? AND checkpoint_armed = 1",
                    (now, str(dispatch_id)),
                )
                return self._queue_row(
                    self._db()
                    .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
        return self.record_recovery_inspection(
            dispatch_id,
            kind="ambiguous",
            next_state="human_attention_required",
            attention_reason=reason,
        )

    def begin_recovery_continuation(
        self,
        dispatch_id: DispatchId | str,
        *,
        epoch: int,
        claim_nonce_sha256: str,
        now: str | None = None,
    ) -> JsonObject:
        """Claim one eligible dead-idle continuation without resetting budgets."""

        if not re.fullmatch(r"[0-9a-f]{64}", claim_nonce_sha256):
            raise ValueError("recovery continuation claim nonce is invalid")
        current = now or utc_now()
        with self._transaction():
            self._require_live_harness_in_transaction(epoch)
            recovery = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if recovery is None or queue is None:
                raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
            if recovery["recovery_state"] != "recovery_continuation_pending":
                raise StaleWriter("recovery continuation is not pending")
            if queue["state"] != "recovery_continuation_pending":
                raise StaleWriter("queue state is not pending recovery continuation")
            live = (
                self._db()
                .execute("SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if live is not None and live["exited_at"] is None:
                raise StaleWriter("recovery continuation requires an exited worker")
            if recovery["next_eligible_at"] is not None and str(recovery["next_eligible_at"]) > current:
                raise StaleWriter("recovery continuation is not yet eligible")
            if int(recovery["continuation_used"]) >= int(recovery["continuation_budget"]):
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'human_attention_required', human_attention_reason = ?, next_eligible_at = NULL, updated_at = ? WHERE dispatch_id = ?",
                    ("continuation budget exhausted", current, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE retry_policies SET revision = revision + 1, strategy = 'human_attention_required', "
                    "next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    ("continuation budget exhausted", current, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'human_attention_required', updated_at = ? WHERE dispatch_id = ?",
                    (current, str(dispatch_id)),
                )
                return self._queue_row(
                    self._db()
                    .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
            fresh = bool(recovery["fresh_thread_after_idle"])
            if fresh and int(recovery["fresh_thread_used"]) >= int(recovery["fresh_thread_budget"]):
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'human_attention_required', human_attention_reason = ?, next_eligible_at = NULL, updated_at = ? WHERE dispatch_id = ?",
                    ("fresh-thread budget exhausted", current, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE retry_policies SET revision = revision + 1, strategy = 'human_attention_required', "
                    "next_eligible_at = NULL, human_attention_reason = ?, updated_at = ? "
                    "WHERE dispatch_id = ?",
                    ("fresh-thread budget exhausted", current, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'human_attention_required', updated_at = ? WHERE dispatch_id = ?",
                    (current, str(dispatch_id)),
                )
                return self._queue_row(
                    self._db()
                    .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
            # A continuation is a new worker process attempt even when it
            # resumes the same logical SDK thread.  Keep ``attempt`` as the
            # monotonic process identity (independent from the bounded
            # continuation budget) and retire the prior immutable capability
            # before binding the new attempt.  The v12 row validator requires
            # every capability to match the queue's current attempt, so the
            # old descriptor cannot remain attached to the active queue.
            self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute(
                "UPDATE recovery_state SET continuation_used = continuation_used + 1, fresh_thread_used = fresh_thread_used + ?, "
                "recovery_state = 'none', next_eligible_at = NULL, inspected_thread_id = NULL, inspected_turn_id = NULL, "
                "inspected_kind = NULL, human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (1 if fresh else 0, current, str(dispatch_id)),
            )
            # Retire the exited worker binding before the continuation claim.
            # A single dispatch may have only one live-owner row, and the
            # validator must never observe an exited binding attached to a
            # newly claimed active queue state.
            self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, last_failure = NULL, strategy = 'none', "
                "next_eligible_at = NULL, prior_thread_id = NULL, prior_turn_id = NULL, "
                "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                (current, str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'claimed', claim_epoch = ?, claim_nonce_sha256 = ?, attempt = attempt + 1, updated_at = ? WHERE dispatch_id = ? AND state = 'recovery_continuation_pending'",
                (epoch, claim_nonce_sha256, current, str(dispatch_id)),
            )
            if fresh:
                self._db().execute(
                    "UPDATE dispatch_queue SET thread_id = NULL WHERE dispatch_id = ?", (str(dispatch_id),)
                )
            return self._queue_row(
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def apply_recovery_action(
        self,
        dispatch_id: DispatchId | str,
        *,
        action_id: str,
        expected_revision: int,
        action_kind: RecoveryActionKind | str,
        reason: str,
        requested_budget: RetryBudgetChange | None = None,
        compatibility_rebind: CompatibilityRebind | None = None,
    ) -> JsonObject:
        """Apply one idempotent, reasoned recovery action by revision CAS."""

        action_id = _command_id(action_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("recovery action expected revision is invalid")
        kind = action_kind if isinstance(action_kind, RecoveryActionKind) else RecoveryActionKind(action_kind)
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512 or "\x00" in reason:
            raise ValueError("recovery action reason is invalid")
        reason = redact_control_text(reason, limit=512)
        if kind is RecoveryActionKind.BUDGET_CHANGE and requested_budget is None:
            raise ValueError("budget-change action requires requested budgets")
        if kind is not RecoveryActionKind.BUDGET_CHANGE and requested_budget is not None:
            raise ValueError("only budget-change action may carry requested budgets")
        if kind is RecoveryActionKind.COMPATIBILITY_REBIND and compatibility_rebind is None:
            raise ValueError("compatibility-rebind action requires exact compatibility facts")
        if kind is not RecoveryActionKind.COMPATIBILITY_REBIND and compatibility_rebind is not None:
            raise ValueError("only compatibility-rebind action may carry compatibility facts")
        budget_json = _encode_json(requested_budget.to_control_json()) if requested_budget is not None else None
        rebind_json = _encode_json(compatibility_rebind.to_json()) if compatibility_rebind is not None else None
        with self._transaction():
            existing = (
                self._db().execute("SELECT * FROM recovery_controls WHERE action_id = ?", (action_id,)).fetchone()
            )
            if existing is not None:
                if (
                    str(existing["dispatch_id"]) != str(dispatch_id)
                    or int(existing["expected_revision"]) != expected_revision
                    or str(existing["action_kind"]) != kind.value
                    or str(existing["reason"]) != reason
                    or existing["requested_budget_json"] != budget_json
                    or existing["compatibility_rebind_json"] != rebind_json
                ):
                    raise StaleWriter("recovery action identity conflicts")
                if kind is RecoveryActionKind.COMPATIBILITY_REBIND:
                    raise StaleWriter("compatibility rebind action was already applied")
                return self._queue_row(existing)
            queue = (
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            policy = (
                self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if queue is None or policy is None:
                raise RecordNotFound(f"recovery dispatch does not exist: {dispatch_id}")
            pending_cancellation = (
                self._db()
                .execute(
                    "SELECT action_id FROM recovery_controls WHERE dispatch_id = ? AND action_kind = 'cancel' "
                    "ORDER BY created_at, action_id LIMIT 1",
                    (str(dispatch_id),),
                )
                .fetchone()
            )
            if pending_cancellation is not None:
                raise StaleWriter("worker cancellation is already pending")
            current_revision = int(policy["revision"])
            if current_revision != expected_revision:
                raise StaleWriter("recovery action revision is stale")
            if str(queue["state"]) in {"completed", "failed", "cancelled", "result_submitted", "finalizing"}:
                raise StaleWriter("terminal queue dispatch rejects recovery action")
            now = utc_now()
            applied_revision = current_revision + 1
            if kind is RecoveryActionKind.BUDGET_CHANGE:
                assert requested_budget is not None
                used = {
                    "pre_identity_budget": int(policy["pre_identity_used"]),
                    "invalid_chain_budget": int(policy["invalid_chain_used"]),
                    "schema_envelope_budget": int(policy["schema_envelope_used"]),
                    "post_identity_loss_budget": int(policy["post_identity_loss_used"]),
                }
                requested = requested_budget.to_control_json()
                if any(int(requested[name]) < used[name] for name in used):
                    raise StaleWriter("recovery budget cannot fall below consumed retries")
                provider_budget = requested_budget.provider_transient_budget
                if provider_budget is None:
                    self._db().execute(
                        "UPDATE retry_policies SET revision = ?, pre_identity_budget = ?, invalid_chain_budget = ?, "
                        "schema_envelope_budget = ?, post_identity_loss_budget = ?, updated_at = ? WHERE dispatch_id = ?",
                        (
                            applied_revision,
                            requested_budget.pre_identity_budget,
                            requested_budget.invalid_chain_budget,
                            requested_budget.schema_envelope_budget,
                            requested_budget.post_identity_loss_budget,
                            now,
                            str(dispatch_id),
                        ),
                    )
                else:
                    recovery = (
                        self._db()
                        .execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),))
                        .fetchone()
                    )
                    live = (
                        self._db()
                        .execute("SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                        .fetchone()
                    )
                    if recovery is None:
                        raise CorruptSchemaError("provider grant recovery authority is missing")
                    if (
                        str(queue["state"]) != "human_attention_required"
                        or str(policy["strategy"]) != RecoveryStrategy.HUMAN_ATTENTION.value
                        or str(recovery["recovery_state"]) != "human_attention_required"
                    ):
                        raise StaleWriter("provider grant requires human-attention state")
                    if live is not None and live["exited_at"] is None:
                        raise StaleWriter("provider grant rejects an active worker lease")
                    if recovery["inspected_kind"] in {"active_writer", "ambiguous", "malformed"}:
                        raise StaleWriter("provider grant rejects an active or ambiguous writer")
                    if int(recovery["inspection_used"]) >= int(recovery["inspection_budget"]):
                        raise StaleWriter("provider grant lacks one remaining thread inspection")
                    if (
                        int(policy["provider_transient_budget"]) != 3
                        or int(policy["provider_transient_grant_used"]) != 0
                        or int(policy["provider_transient_used"]) < 3
                        or policy["last_failure"] != RetryFailureClass.PROVIDER_TRANSIENT.value
                        or policy["prior_thread_id"] is None
                        or policy["prior_thread_id"] != queue["thread_id"]
                        or int(recovery["continuation_budget"]) != 3
                        or recovery["inspected_thread_id"] is not None
                        or recovery["inspected_turn_id"] is not None
                        or recovery["inspected_kind"] is not None
                        or provider_budget != 4
                        or any(int(requested[name]) != int(policy[name]) for name in used)
                    ):
                        raise StaleWriter("provider grant is not the exact one-step authorization")
                    self._db().execute(
                        "UPDATE retry_policies SET revision = ?, provider_transient_budget = 4, "
                        "provider_transient_used = 4, provider_transient_grant_used = 1, "
                        "strategy = 'same_thread_continuation', "
                        "next_eligible_at = NULL, human_attention_reason = NULL, updated_at = ? "
                        "WHERE dispatch_id = ?",
                        (applied_revision, now, str(dispatch_id)),
                    )
                    self._db().execute(
                        "UPDATE recovery_state SET recovery_state = 'recovery_inspection_pending', "
                        "continuation_budget = 4, "
                        "next_eligible_at = NULL, human_attention_reason = NULL, updated_at = ? "
                        "WHERE dispatch_id = ?",
                        (now, str(dispatch_id)),
                    )
                    self._db().execute(
                        "UPDATE dispatch_queue SET state = 'recovery_inspection_pending', updated_at = ? "
                        "WHERE dispatch_id = ? AND state = 'human_attention_required'",
                        (now, str(dispatch_id)),
                    )
            elif kind is RecoveryActionKind.RETRY:
                if (
                    str(queue["state"]) != "human_attention_required"
                    or str(policy["strategy"]) != RecoveryStrategy.HUMAN_ATTENTION.value
                ):
                    raise StaleWriter("retry action requires human-attention state")
                self._db().execute(
                    "UPDATE retry_policies SET revision = ?, strategy = 'none', human_attention_reason = NULL, "
                    "next_eligible_at = ?, updated_at = ? WHERE dispatch_id = ?",
                    (applied_revision, now, now, str(dispatch_id)),
                )
                self._db().execute("DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(dispatch_id),))
                self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                self._db().execute(
                    "UPDATE control_commands SET state = 'unresolved', acknowledged_at = COALESCE(acknowledged_at, ?) "
                    "WHERE dispatch_id = ? AND state IN ('pending', 'sent')",
                    (now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, claim_nonce_sha256 = NULL, "
                    "attempt = attempt + 1, thread_id = NULL, host_id = NULL, available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                    (now, now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, "
                    "inspected_thread_id = NULL, inspected_turn_id = NULL, inspected_kind = NULL, "
                    "human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                    (now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? "
                    "WHERE dispatch_id = ? AND checkpoint_armed = 1",
                    (now, str(dispatch_id)),
                )
            elif kind is RecoveryActionKind.COMPATIBILITY_REBIND:
                assert compatibility_rebind is not None
                if compatibility_rebind.expected_profile_sha256 is None:
                    raise ValueError("new compatibility rebind requires the complete runtime identity tuple")
                if (
                    str(queue["state"]) != "human_attention_required"
                    or str(policy["strategy"]) != RecoveryStrategy.HUMAN_ATTENTION.value
                ):
                    raise StaleWriter("compatibility rebind requires human-attention state")
                if (int(queue["generation"]), int(queue["attempt"])) != (
                    compatibility_rebind.expected_generation,
                    compatibility_rebind.expected_attempt,
                ):
                    raise StaleWriter("compatibility rebind worker identity is stale")
                live = (
                    self._db()
                    .execute("SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
                if live is not None and live["exited_at"] is None:
                    raise StaleWriter("compatibility rebind rejects an active worker lease")
                active_controller = (
                    self._db()
                    .execute(
                        "SELECT 1 FROM controller_decisions d "
                        "JOIN controller_decision_generations g ON g.decision_id = d.decision_id "
                        "WHERE d.dispatch_id = ? AND (g.state IN ('delivery_starting','active') "
                        "OR (d.claim_token_sha256 IS NOT NULL AND d.claim_lease_expires_at > ?)) LIMIT 1",
                        (str(dispatch_id), now),
                    )
                    .fetchone()
                )
                if active_controller is not None:
                    raise StaleWriter("compatibility rebind rejects an active controller turn or lease")
                current_capability = (
                    self._db()
                    .execute(
                        "SELECT consumed_at FROM attempt_capabilities "
                        "WHERE dispatch_id = ? AND generation = ? AND attempt = ?",
                        (
                            str(dispatch_id),
                            compatibility_rebind.expected_generation,
                            compatibility_rebind.expected_attempt,
                        ),
                    )
                    .fetchone()
                )
                revoke_exited_attempt = (
                    current_capability is not None
                    and current_capability["consumed_at"] is None
                    and live is not None
                    and live["exited_at"] is not None
                )
                if current_capability is not None and not revoke_exited_attempt:
                    raise StaleWriter("compatibility rebind rejects an issued worker attempt")
                binding = (
                    self._db()
                    .execute("SELECT * FROM queue_bindings WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
                if binding is None:
                    raise CorruptSchemaError("compatibility rebind queue binding is missing")
                route = _decode_json_object(str(queue["route_json"]), field_name="compatibility rebind route")
                old_profile = compatibility_rebind.expected_profile_sha256
                old_compatibility = compatibility_rebind.expected_compatibility_sha256
                recovery = (
                    self._db()
                    .execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
                if recovery is None:
                    raise CorruptSchemaError("compatibility rebind recovery authority is missing")
                if (
                    route is None
                    or route.get("native_profile_sha256") != old_profile
                    or binding["native_profile_sha256"] != old_profile
                    or recovery["profile_sha256"] != old_profile
                    or route.get("native_compatibility_sha256") != old_compatibility
                    or binding["native_compatibility_sha256"] != old_compatibility
                ):
                    raise StaleWriter("compatibility rebind old identity tuple does not match queue authority")
                if revoke_exited_attempt:
                    self._db().execute(
                        "DELETE FROM attempt_capabilities "
                        "WHERE dispatch_id = ? AND generation = ? AND attempt = ? AND consumed_at IS NULL",
                        (
                            str(dispatch_id),
                            compatibility_rebind.expected_generation,
                            compatibility_rebind.expected_attempt,
                        ),
                    )
                rebound_route = dict(route)
                rebound_route["native_profile_sha256"] = compatibility_rebind.proposed_profile_sha256
                rebound_route["native_compatibility_sha256"] = compatibility_rebind.proposed_compatibility_sha256
                self._db().execute(
                    "UPDATE dispatch_queue SET route_json = ?, updated_at = ? WHERE dispatch_id = ?",
                    (_encode_json(rebound_route), now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET native_profile_sha256 = ?, native_compatibility_sha256 = ?, "
                    "updated_at = ? WHERE dispatch_id = ?",
                    (
                        compatibility_rebind.proposed_profile_sha256,
                        compatibility_rebind.proposed_compatibility_sha256,
                        now,
                        str(dispatch_id),
                    ),
                )
                self._db().execute(
                    "UPDATE recovery_state SET profile_sha256 = ?, updated_at = ? WHERE dispatch_id = ?",
                    (compatibility_rebind.proposed_profile_sha256, now, str(dispatch_id)),
                )
                self._db().execute(
                    "UPDATE retry_policies SET revision = ?, updated_at = ? WHERE dispatch_id = ?",
                    (applied_revision, now, str(dispatch_id)),
                )
            else:
                live = (
                    self._db()
                    .execute("SELECT * FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                    .fetchone()
                )
                if live is not None and live["exited_at"] is None:
                    # The reasoned action is now durable and wins the policy
                    # CAS, but terminal authority must wait for the exact
                    # harness-owned child exit acknowledgement.
                    self._db().execute(
                        "UPDATE retry_policies SET revision = ?, updated_at = ? WHERE dispatch_id = ?",
                        (applied_revision, now, str(dispatch_id)),
                    )
                else:
                    if live is None and str(queue["state"]) in {"claimed", "starting", "running"}:
                        raise StaleWriter("live cancellation requires durable worker ownership")
                    self._cancel_queue_row_in_transaction(
                        queue,
                        epoch=None,
                        retain_exited_liveness=live is not None,
                    )
            self._db().execute(
                "INSERT INTO recovery_controls(action_id, dispatch_id, expected_revision, action_kind, reason, requested_budget_json, compatibility_rebind_json, applied_revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    str(dispatch_id),
                    expected_revision,
                    kind.value,
                    reason,
                    budget_json,
                    rebind_json,
                    applied_revision,
                    now,
                ),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM recovery_controls WHERE action_id = ?", (action_id,)).fetchone()
            )

    def begin_recovery_inspection(
        self,
        dispatch_id: DispatchId | str,
        *,
        epoch: int,
        now: str | None = None,
    ) -> JsonObject:
        """Claim one eligible retry inspection without creating an SDK writer."""

        current = now or utc_now()
        with self._transaction():
            self._require_live_harness_in_transaction(epoch)
            recovery = (
                self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )
            if recovery is None:
                raise RecordNotFound(f"recovery state does not exist: {dispatch_id}")
            if recovery["recovery_state"] != "recovery_retry_wait":
                raise StaleWriter("recovery inspection is not pending")
            queue = (
                self._db()
                .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if queue["state"] != "recovery_retry_wait":
                raise StaleWriter("queue state is not pending recovery inspection")
            live = (
                self._db()
                .execute("SELECT exited_at FROM worker_liveness WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if live is not None and live["exited_at"] is None:
                raise StaleWriter("recovery inspection requires an exited worker")
            if recovery["next_eligible_at"] is not None and str(recovery["next_eligible_at"]) > current:
                raise StaleWriter("recovery inspection is not yet eligible")
            self._db().execute(
                "UPDATE recovery_state SET recovery_state = 'recovery_inspection_pending', next_eligible_at = NULL, updated_at = ? WHERE dispatch_id = ?",
                (current, str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE retry_policies SET revision = revision + 1, updated_at = ? WHERE dispatch_id = ?",
                (current, str(dispatch_id)),
            )
            self._db().execute(
                "UPDATE dispatch_queue SET state = 'recovery_inspection_pending', updated_at = ? WHERE dispatch_id = ? AND state = 'recovery_retry_wait'",
                (current, str(dispatch_id)),
            )
            return self._queue_row(
                self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),)).fetchone()
            )

    def _require_live_harness_in_transaction(self, epoch: int) -> None:
        row = self._db().execute("SELECT epoch, expires_at FROM harness_authority WHERE singleton = 1").fetchone()
        if row is None or int(row["epoch"]) != epoch or str(row["expires_at"]) <= utc_now():
            raise StaleWriter("operation requires a live harness lease")

    def claim_due_checkpoint(self, *, epoch: int, now: str | None = None) -> JsonObject | None:
        current = now or utc_now()
        with self._transaction():
            self._require_live_harness_in_transaction(epoch)
            row = (
                self._db()
                .execute(
                    "SELECT q.*, b.source_thread_id, b.checkpoint_deadline, l.pid, l.process_birth_identity, "
                    "l.last_seen_at, l.lease_expires_at FROM dispatch_queue q JOIN queue_bindings b "
                    "ON b.dispatch_id = q.dispatch_id LEFT JOIN worker_liveness l ON l.dispatch_id = q.dispatch_id "
                    "WHERE q.state IN ('claimed','starting','running','result_submitted','human_attention_required') "
                    "AND b.checkpoint_armed = 1 AND b.checkpoint_deadline IS NOT NULL AND b.checkpoint_deadline <= ? "
                    "ORDER BY b.checkpoint_deadline, q.sequence LIMIT 1",
                    (current,),
                )
                .fetchone()
            )
            if row is None:
                return None
            dispatch_id = str(row["dispatch_id"])
            latest_sequence = int(
                self._db()
                .execute(
                    "SELECT COALESCE(MAX(cycle_sequence), 0) FROM wake_outbox WHERE dispatch_id = ? AND kind = 'checkpoint'",
                    (dispatch_id,),
                )
                .fetchone()[0]
            )
            cycle_sequence = latest_sequence + 1
            if cycle_sequence > 64:
                self._db().execute(
                    "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? WHERE dispatch_id = ?",
                    (current, dispatch_id),
                )
                raise StaleWriter("checkpoint cycle budget exhausted")
            delivery_id = (
                f"wake/{dispatch_id}/checkpoint"
                if cycle_sequence == 1
                else f"wake/{dispatch_id}/checkpoint/{cycle_sequence}"
            )
            self._db().execute(
                "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? "
                "WHERE dispatch_id = ? AND checkpoint_armed = 1",
                (current, dispatch_id),
            )
            payload = {
                "schema_version": 1,
                "kind": "CHECKPOINT",
                "delivery_id": delivery_id,
                "dispatch_id": dispatch_id,
                "cycle_sequence": cycle_sequence,
                "state": str(row["state"]),
                "thread_id": row["thread_id"],
                "pid": row["pid"],
                "process_birth_identity": row["process_birth_identity"],
                "started_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "lease_expires_at": row["lease_expires_at"],
                "ledger_path": os.fspath(self.path),
            }
            encoded = _encode_json(payload)
            source_thread = row["source_thread_id"]
            state = "pending" if source_thread is not None else "not_applicable"
            decision_id = self._ensure_controller_decision_in_transaction(
                dispatch_id=dispatch_id,
                kind="checkpoint",
                cycle_sequence=cycle_sequence,
                payload=payload,
                source_thread_id=source_thread,
                deadline=self._expires_after(current, _CONTROLLER_DECISION_WINDOW_SECONDS),
                wake_state=state,
                queue_state=str(row["state"]),
            )
            self._db().execute(
                "INSERT INTO wake_outbox(delivery_id, dispatch_id, decision_id, kind, cycle_sequence, source_thread_id, payload_json, payload_digest, state, attempt_count, source_turn_id, created_at, updated_at) VALUES (?, ?, ?, 'checkpoint', ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
                (
                    delivery_id,
                    dispatch_id,
                    decision_id,
                    cycle_sequence,
                    source_thread,
                    encoded,
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    state,
                    current,
                    current,
                ),
            )
            wake = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._queue_row(wake)

    def rearm_checkpoint(self, dispatch_id: DispatchId | str, *, seconds: float = 1800.0) -> JsonObject:
        now = utc_now()
        deadline = self._expires_after(now, seconds)
        with self._transaction():
            queue = (
                self._db()
                .execute("SELECT state FROM dispatch_queue WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if queue is None:
                raise RecordNotFound(f"queue dispatch does not exist: {dispatch_id}")
            if queue["state"] in {"completed", "failed", "cancelled"}:
                raise StaleWriter("terminal dispatch cannot re-arm a checkpoint")
            existing = (
                self._db()
                .execute(
                    "SELECT state FROM wake_outbox WHERE dispatch_id = ? AND kind = 'checkpoint' ORDER BY cycle_sequence DESC LIMIT 1",
                    (str(dispatch_id),),
                )
                .fetchone()
            )
            if existing is not None and existing["state"] in {"pending", "starting"}:
                raise StaleWriter("checkpoint wake is already active")
            latest_decision = (
                self._db()
                .execute(
                    "SELECT state FROM controller_decisions WHERE dispatch_id = ? AND kind = 'checkpoint' "
                    "ORDER BY cycle_sequence DESC LIMIT 1",
                    (str(dispatch_id),),
                )
                .fetchone()
            )
            if latest_decision is not None and latest_decision["state"] not in {
                ControllerDecisionState.AWAITING_CLAIM.value,
                ControllerDecisionState.ACKNOWLEDGED.value,
                ControllerDecisionState.SUPERSEDED.value,
                ControllerDecisionState.LEGACY_CLOSED.value,
                ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value,
            }:
                raise StaleWriter("checkpoint decision is still active")
            binding = (
                self._db()
                .execute("SELECT checkpoint_armed FROM queue_bindings WHERE dispatch_id = ?", (str(dispatch_id),))
                .fetchone()
            )
            if binding is None:
                raise CorruptSchemaError("controller checkpoint binding is missing")
            if int(binding["checkpoint_armed"]) == 1:
                raise StaleWriter("checkpoint is already armed")
            self._db().execute(
                "UPDATE queue_bindings SET checkpoint_deadline = ?, checkpoint_armed = 1, updated_at = ? WHERE dispatch_id = ?",
                (deadline, now, str(dispatch_id)),
            )
            return self.queue_binding(dispatch_id)

    # ------------------------------------------------------------------
    # v15 controller decision authority
    # ------------------------------------------------------------------

    def _decision_status_from_row(
        self, row: sqlite3.Row, summary: sqlite3.Row | None = None
    ) -> ControllerDecisionStatus:
        summary_value = strict_json_loads(str(row["summary_json"]), max_bytes=16_384)
        if not isinstance(summary_value, dict):
            raise CorruptSchemaError("controller decision summary is not an object")
        successor_values = summary_value.get("expected_successor_dispatch_ids", [])
        if not isinstance(successor_values, list) or any(not isinstance(item, str) for item in successor_values):
            successor_values = []
        dispatch_id = str(row["dispatch_id"])
        queue = self._db().execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
        policy = self._db().execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
        recovery = self._db().execute("SELECT * FROM recovery_state WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
        if queue is None or policy is None or recovery is None:
            raise CorruptSchemaError("controller decision context is incomplete")
        meaningful_activity = [item for item in self.recent_activity(dispatch_id) if item.get("text") is not None]
        progress_activity = [
            item
            for item in meaningful_activity
            if str(item["text"]).lstrip().startswith("{") or "agentMessage" in str(item["kind"])
        ][-2:]
        selected_activity = {int(item["sequence"]): item for item in (*progress_activity, *meaningful_activity[-6:])}
        activity_rows = [selected_activity[key] for key in sorted(selected_activity)][-8:]
        recent_activity = tuple(
            DiagnosticEvent(
                int(item["sequence"]),
                str(item["kind"]),
                str(item["occurred_at"]),
                redact_diagnostic_text(str(item["text"]), limit=1_024),
                str(item["payload_sha256"]) if item.get("payload_sha256") is not None else None,
            )
            for item in activity_rows
        )
        attention_reason = policy["human_attention_reason"] or recovery["human_attention_reason"]
        prior_thread_id = policy["prior_thread_id"] or queue["thread_id"] or recovery["inspected_thread_id"]
        prior_turn_id = policy["prior_turn_id"] or recovery["inspected_turn_id"]
        decision_summary = ControllerDecisionSummary(
            DispatchId(dispatch_id),
            str(row["kind"]),
            ControllerDecisionState(str(row["state"])),
            str(summary_value.get("summary", summary_value.get("state", "controller decision"))),
            str(row["source_thread_id"]) if row["source_thread_id"] is not None else None,
            str(row["deadline"]),
            int(row["revision"]),
            tuple(successor_values),
            str(queue["state"]),
            int(policy["revision"]),
            RecoveryStrategy(str(policy["strategy"])),
            RetryFailureClass(str(policy["last_failure"])) if policy["last_failure"] is not None else None,
            str(attention_reason) if attention_reason is not None else None,
            ThreadIdentity(str(prior_thread_id)) if prior_thread_id is not None else None,
            str(prior_turn_id) if prior_turn_id is not None else None,
            str(recovery["recovery_state"]),
            str(recovery["worker_exit_classification"]) if recovery["worker_exit_classification"] is not None else None,
            int(recovery["exit_code"]) if recovery["exit_code"] is not None else None,
            recent_activity,
        )
        return ControllerDecisionStatus(
            ControllerDecisionId(str(row["decision_id"])),
            DispatchId(str(row["dispatch_id"])),
            str(row["kind"]),
            ControllerDecisionState(str(row["state"])),
            int(row["revision"]),
            Generation(int(row["current_generation"])),
            int(row["generation_budget"]),
            int(row["generation_used"]),
            ControllerClaimantKind(str(row["claimant_kind"])) if row["claimant_kind"] is not None else None,
            str(row["claimant_id"]) if row["claimant_id"] is not None else None,
            str(row["claim_lease_expires_at"]) if row["claim_lease_expires_at"] is not None else None,
            str(row["action_id"]) if row["action_id"] is not None else None,
            str(row["action_bundle_sha256"]) if row["action_bundle_sha256"] is not None else None,
            str(row["deadline"]),
            decision_summary,
        )

    @staticmethod
    def _program_graph_projection(program: ProgramGraph) -> JsonObject:
        """Serialize one executable graph without making the model its authority."""

        # ``capsule_json`` is the existing execution-capsule projection.  The
        # import is local because controller.py already owns that projection
        # and ledger.py must not create a second serializer.
        from .controller import capsule_json

        return {
            "schema": "codex-flow/program-graph/v1",
            "program_id": str(program.program_id),
            "plan_path": os.fspath(program.plan_path),
            "plan_revision_sha256": program.plan_revision_sha256,
            "trunk_head": program.trunk_head,
            "integration_strategy": program.integration_strategy,
            "nodes": [
                {
                    "milestone_id": str(node.milestone_id),
                    "dependencies": [str(item) for item in node.dependencies],
                    "mutable_surfaces": list(node.mutable_surfaces),
                    "capsule": capsule_json(node.capsule),
                }
                for node in program.nodes
            ],
        }

    @staticmethod
    def _program_node_payloads(
        graph_value: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        if graph_value.get("schema") != "codex-flow/program-graph/v1":
            raise CorruptSchemaError("program graph schema is invalid")
        raw_nodes = graph_value.get("nodes")
        if not isinstance(raw_nodes, list):
            raise CorruptSchemaError("program graph nodes are invalid")
        nodes: dict[str, dict[str, object]] = {}
        for raw in raw_nodes:
            if not isinstance(raw, dict) or not isinstance(raw.get("milestone_id"), str):
                raise CorruptSchemaError("program graph node is invalid")
            node_id = str(raw["milestone_id"])
            if node_id in nodes:
                raise CorruptSchemaError("program graph contains duplicate nodes")
            dependencies = raw.get("dependencies")
            surfaces = raw.get("mutable_surfaces")
            if (
                not isinstance(dependencies, list)
                or any(not isinstance(item, str) for item in dependencies)
                or not isinstance(surfaces, list)
                or any(not isinstance(item, str) for item in surfaces)
            ):
                raise CorruptSchemaError("program graph node ownership facts are invalid")
            nodes[node_id] = raw
        return nodes

    def _program_status_from_row(self, row: sqlite3.Row) -> ProgramStatus:
        if row["program_digest"] is None or row["program_graph_json"] is None or row["trunk_head"] is None:
            raise CorruptSchemaError("run is not a registered program")
        try:
            graph_value = strict_json_loads(str(row["program_graph_json"]), max_bytes=2_000_000)
        except ValueError as exc:
            raise CorruptSchemaError("program graph projection is invalid") from exc
        if not isinstance(graph_value, dict):
            raise CorruptSchemaError("program graph projection is not an object")
        nodes = self._program_node_payloads(graph_value)
        statuses: list[ProgramNodeStatus] = []
        for milestone_id, payload in sorted(nodes.items()):
            milestone = (
                self._db()
                .execute(
                    "SELECT current_state FROM milestones WHERE run_id = ? AND milestone_id = ?",
                    (str(row["run_id"]), milestone_id),
                )
                .fetchone()
            )
            if milestone is None:
                raise CorruptSchemaError("program graph node has no milestone row")
            dependencies = tuple(MilestoneId(item) for item in payload["dependencies"])
            reviews: list[str] = []
            findings: list[str] = []
            candidate: str | None = None
            candidate_disposition: CandidateDisposition | None = None
            blocker: TypedBlocker | None = None
            for fact in self.review_lifecycle(row["run_id"], milestone_id):
                value = fact.data
                if isinstance(value.get("candidate_sha"), str):
                    if candidate != value["candidate_sha"]:
                        # A same-owner repair/adoption may supersede a
                        # rejected candidate.  Blockers belong to the exact
                        # candidate identity and must not leak forward.
                        blocker = None
                    candidate = str(value["candidate_sha"])
                    candidate_disposition = CandidateDisposition.VERIFIED_COMMIT
                raw_disposition = value.get("candidate_disposition")
                if isinstance(raw_disposition, str):
                    try:
                        candidate_disposition = CandidateDisposition(raw_disposition)
                    except ValueError as exc:
                        raise CorruptSchemaError("program candidate disposition is invalid") from exc
                raw_blocker = value.get("blocker")
                if raw_blocker is not None and value.get("candidate_sha") == candidate:
                    if not isinstance(raw_blocker, Mapping):
                        raise CorruptSchemaError("program blocker projection is invalid")
                    try:
                        blocker = TypedBlocker.from_json(raw_blocker)
                    except (TypeError, ValueError) as exc:
                        raise CorruptSchemaError("program blocker projection is invalid") from exc
                if fact.kind == "candidate_blocker_resolved" and value.get("candidate_sha") == candidate:
                    gate_id = value.get("blocker_gate_id")
                    resolution = value.get("blocker_resolution")
                    if (
                        not isinstance(gate_id, str)
                        or resolution not in {"resolve", "supersede"}
                        or blocker is None
                        or blocker.gate_id != gate_id
                    ):
                        raise CorruptSchemaError("program blocker resolution is stale or malformed")
                    blocker = None
                review_id = value.get("review_id")
                if isinstance(review_id, str):
                    reviews.append(review_id)
                finding_ids = value.get("finding_ids")
                if isinstance(finding_ids, list):
                    findings.extend(item for item in finding_ids if isinstance(item, str))
            integration = (
                self._db()
                .execute(
                    "SELECT candidate_sha, state FROM integration_outbox WHERE program_id = ? AND milestone_id = ? "
                    "ORDER BY created_at DESC, integration_id DESC LIMIT 1",
                    (str(row["run_id"]), milestone_id),
                )
                .fetchone()
            )
            integrated = integration is not None and str(integration["state"]) == "applied"
            if integration is not None and candidate is None:
                candidate = str(integration["candidate_sha"])
            statuses.append(
                ProgramNodeStatus(
                    MilestoneId(milestone_id),
                    str(milestone["current_state"]),
                    dependencies,
                    candidate,
                    tuple(reviews),
                    tuple(findings),
                    integrated,
                    candidate_disposition,
                    blocker,
                )
            )
        try:
            state = ProgramState(str(row["program_state"]))
        except ValueError as exc:
            raise CorruptSchemaError("program state is invalid") from exc
        return ProgramStatus(
            ProgramId(str(row["run_id"])),
            state,
            int(row["program_revision"]),
            str(row["program_digest"]),
            str(row["trunk_head"]),
            tuple(statuses),
        )

    def register_program(self, program: ProgramGraph) -> ProgramStatus:
        """Register one complete graph idempotently on the existing run authority."""

        if not isinstance(program, ProgramGraph):
            raise TypeError("program must be a typed ProgramGraph")
        projection = self._program_graph_projection(program)
        encoded = _encode_json(projection)
        # The canonical plan is the static intent authority.  The graph bytes
        # are retained as a closed projection, but must not replace the plan
        # revision digest in the durable program identity.
        digest = program.plan_revision_sha256
        now = utc_now()
        with self._transaction():
            existing = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program.program_id),)).fetchone()
            if existing is not None and existing["program_digest"] is not None:
                if (
                    str(existing["program_digest"]) != digest
                    or str(existing["program_graph_json"]) != encoded
                    or str(existing["trunk_head"]) != program.trunk_head
                ):
                    raise StaleWriter("program registration conflicts with its durable graph")
                return self._program_status_from_row(existing)
            if existing is None:
                self._db().execute(
                    "INSERT INTO runs(run_id, created_at, closed_at, metadata_json, program_digest, program_revision, "
                    "program_state, program_graph_json, trunk_head) VALUES (?, ?, NULL, '{}', ?, 0, 'registered', ?, ?)",
                    (str(program.program_id), now, digest, encoded, program.trunk_head),
                )
            else:
                if (
                    self._db()
                    .execute("SELECT 1 FROM dispatches WHERE run_id = ? LIMIT 1", (str(program.program_id),))
                    .fetchone()
                    is not None
                ):
                    raise StaleWriter("existing run already owns dispatches and cannot become a program")
                self._db().execute(
                    "UPDATE runs SET program_digest = ?, program_revision = 0, program_state = 'registered', "
                    "program_graph_json = ?, trunk_head = ? WHERE run_id = ?",
                    (digest, encoded, program.trunk_head, str(program.program_id)),
                )
            for node in program.nodes:
                milestone = (
                    self._db()
                    .execute(
                        "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                        (str(program.program_id), str(node.milestone_id)),
                    )
                    .fetchone()
                )
                if milestone is None:
                    self._db().execute(
                        "INSERT INTO milestones(run_id, milestone_id, current_state, created_at, updated_at, metadata_json) "
                        "VALUES (?, ?, 'PLANNED', ?, ?, '{}')",
                        (str(program.program_id), str(node.milestone_id), now, now),
                    )
                elif str(milestone["current_state"]) != WorkflowState.PLANNED.value:
                    raise StaleWriter("program registration requires planned milestone rows")
                for dependency in node.dependencies:
                    self._db().execute(
                        "INSERT INTO milestone_dependencies(program_id, milestone_id, dependency_milestone_id, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (str(program.program_id), str(node.milestone_id), str(dependency), now),
                    )
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program.program_id),)).fetchone()
            assert row is not None
            return self._program_status_from_row(row)

    def program_status(self, program_id: ProgramId | str) -> ProgramStatus:
        identity = ProgramId(str(program_id))
        row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(identity),)).fetchone()
        if row is None:
            raise RecordNotFound(f"program does not exist: {identity}")
        return self._program_status_from_row(row)

    def program_controller_context(
        self,
        program_id: ProgramId | str,
        *,
        expected_revision: int,
        expected_trunk_head: str,
    ) -> ProgramControllerContext:
        """Read one revision-bound complete controller DAG projection."""

        identity = ProgramId(str(program_id))
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("program context revision is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", expected_trunk_head) is None:
            raise ValueError("program context trunk HEAD is invalid")
        with self._transaction():
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(identity),)).fetchone()
            if row is None or row["program_digest"] is None:
                raise RecordNotFound(f"program does not exist: {identity}")
            if int(row["program_revision"]) != expected_revision or str(row["trunk_head"]) != expected_trunk_head:
                raise StaleWriter("program context identity is stale")
            graph = self.program_graph(identity)
            status = self._program_status_from_row(row)
            status_by_id = {node.milestone_id: node for node in status.nodes}
            ready = set(status.ready_milestones)
            role_by_mode = {
                AcceptanceMode.OBJECTIVE: "code-reviewer",
                AcceptanceMode.VISUAL: "visual-reviewer",
                AcceptanceMode.ARCHITECTURE: "architecture-reviewer",
            }
            nodes = tuple(
                ProgramControllerNodeContext(
                    node.milestone_id,
                    status_by_id[node.milestone_id].state,
                    node.dependencies,
                    node.mutable_surfaces,
                    node.capsule.acceptance_modes,
                    tuple(sorted(role_by_mode[mode] for mode in node.capsule.acceptance_modes)),
                    status_by_id[node.milestone_id].candidate_sha,
                    status_by_id[node.milestone_id].review_ids,
                    status_by_id[node.milestone_id].finding_ids,
                    status_by_id[node.milestone_id].integrated,
                    node.milestone_id in ready,
                    status_by_id[node.milestone_id].candidate_disposition,
                    status_by_id[node.milestone_id].blocker,
                )
                for node in graph.nodes
            )
            return ProgramControllerContext(
                identity,
                expected_revision,
                str(row["program_digest"]),
                graph.plan_path,
                expected_trunk_head,
                nodes,
            )

    def program_graph(self, program_id: ProgramId | str) -> ProgramGraph:
        """Rehydrate the immutable graph projection after a controller restart."""

        identity = ProgramId(str(program_id))
        row = (
            self._db()
            .execute("SELECT * FROM runs WHERE run_id = ? AND program_digest IS NOT NULL", (str(identity),))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"program does not exist: {identity}")
        try:
            graph_value = strict_json_loads(str(row["program_graph_json"]), max_bytes=2_000_000)
            if not isinstance(graph_value, dict):
                raise ValueError("program graph is not an object")
            raw_nodes = self._program_node_payloads(graph_value)
            from .controller import capsule_from_json

            nodes = tuple(
                ProgramNodeSpec(
                    MilestoneId(milestone_id),
                    capsule_from_json(payload["capsule"]),  # type: ignore[arg-type]
                    tuple(MilestoneId(item) for item in payload["dependencies"]),
                )
                for milestone_id, payload in sorted(raw_nodes.items())
            )
            return ProgramGraph(
                identity,
                Path(str(graph_value["plan_path"])),
                str(graph_value["plan_revision_sha256"]),
                nodes,
                str(graph_value["trunk_head"]),
                str(graph_value["integration_strategy"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptSchemaError("program graph projection cannot be rehydrated") from exc

    def program_ready_milestones(self, program_id: ProgramId | str) -> tuple[MilestoneId, ...]:
        return self.program_status(program_id).ready_milestones

    def start_program(
        self, program_id: ProgramId | str, *, event_key: str = "start"
    ) -> ProgramControllerDecisionStatus:
        identity = ProgramId(str(program_id))
        with self._transaction():
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(identity),)).fetchone()
            if row is None:
                raise RecordNotFound(f"program does not exist: {identity}")
            if row["program_digest"] is None:
                raise StaleWriter("run is not registered as a program")
            current = str(row["program_state"])
            if current in {ProgramState.COMPLETED.value, ProgramState.FAILED.value}:
                raise StaleWriter("program is terminal")
            if current == ProgramState.REGISTERED.value:
                self._db().execute(
                    "UPDATE runs SET program_state = 'running', program_revision = program_revision + 1 WHERE run_id = ?",
                    (str(identity),),
                )
            return self._ensure_program_decision_in_transaction(
                identity,
                event_kind=ProgramEventKind.CHECKPOINT,
                event_key=event_key,
                payload={
                    "summary": "program start requested",
                    "ready_milestones": [str(item) for item in self.program_ready_milestones(identity)],
                },
            )

    def _ensure_program_decision_in_transaction(
        self,
        program_id: ProgramId,
        *,
        event_kind: ProgramEventKind,
        event_key: str,
        payload: Mapping[str, object],
        deadline: str | None = None,
    ) -> ProgramControllerDecisionStatus:
        if not isinstance(event_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", event_key):
            raise ValueError("program event key is invalid")
        program = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program_id),)).fetchone()
        if program is None or program["program_digest"] is None:
            raise RecordNotFound(f"program does not exist: {program_id}")
        decision_id = ControllerDecisionId(f"decision/program/{program_id}/{event_kind.value}/{event_key}")
        existing = (
            self._db()
            .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(decision_id),))
            .fetchone()
        )
        if existing is not None:
            return self._program_decision_status_from_row(existing)
        summary: JsonObject = {
            "schema_version": 1,
            "program_id": str(program_id),
            "event_kind": event_kind.value,
            "event_key": event_key,
            "program_revision": int(program["program_revision"]),
            "plan_digest": str(program["program_digest"]),
            "trunk_head": str(program["trunk_head"]),
            "payload": dict(payload),
        }
        summary_json = _encode_json(summary)
        now = utc_now()
        decision_deadline = deadline or self._expires_after(now, _CONTROLLER_DECISION_WINDOW_SECONDS)
        self._db().execute(
            "INSERT INTO controller_decisions(decision_id, dispatch_id, program_id, program_revision, event_kind, event_key, "
            "kind, cycle_sequence, summary_json, summary_sha256, source_thread_id, state, revision, current_generation, "
            "generation_budget, generation_used, claimant_kind, claimant_id, claim_token_sha256, claim_started_at, "
            "claim_lease_expires_at, action_id, action_bundle_json, action_bundle_sha256, committed_at, acknowledged_at, "
            "superseded_at, deadline, human_attention_reason, created_at, updated_at) VALUES (?, NULL, ?, ?, ?, ?, 'terminal', 1, ?, ?, NULL, 'awaiting_claim', 0, 1, 2, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)",
            (
                str(decision_id),
                str(program_id),
                int(program["program_revision"]),
                event_kind.value,
                event_key,
                summary_json,
                hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
                decision_deadline,
                now,
                now,
            ),
        )
        self._db().execute(
            "INSERT INTO controller_decision_generations(decision_id, generation, lineage_id, predecessor_generation, source_kind, state, prompt_sha256, controller_thread_id, controller_turn_id, inspection_started_at, inspection_token_sha256, inspection_lease_expires_at, inspection_completed_at, inspection_outcome, inspection_bundle_json, inspection_bundle_sha256, started_at, terminal_at, created_at, updated_at) VALUES (?, 1, ?, NULL, 'source_controller', 'prepared', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (str(decision_id), f"lineage/{decision_id}", now, now),
        )
        row = (
            self._db()
            .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(decision_id),))
            .fetchone()
        )
        assert row is not None
        return self._program_decision_status_from_row(row)

    def record_program_event(
        self,
        program_id: ProgramId | str,
        event_kind: ProgramEventKind | str,
        event_key: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ProgramControllerDecisionStatus:
        identity = ProgramId(str(program_id))
        kind = event_kind if isinstance(event_kind, ProgramEventKind) else ProgramEventKind(event_kind)
        with self._transaction():
            if kind is ProgramEventKind.CHECKPOINT:
                now = utc_now()
                # An ACTIVE inspection is a durable deferral, not a terminal
                # program revision.  Only a newly armed explicit checkpoint
                # may retire that claim and make another controller decision
                # on the same program revision claimable.
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'superseded', superseded_at = ?, "
                    "claim_lease_expires_at = NULL, human_attention_reason = NULL, updated_at = ? "
                    "WHERE program_id = ? AND program_revision = ("
                    "SELECT program_revision FROM runs WHERE run_id = ?) "
                    "AND action_id IS NULL AND state IN ('awaiting_claim','claimed','human_attention_required') "
                    "AND EXISTS (SELECT 1 FROM controller_decision_generations g "
                    "WHERE g.decision_id = controller_decisions.decision_id "
                    "AND g.generation = controller_decisions.current_generation "
                    "AND g.inspection_outcome = 'active')",
                    (now, now, str(identity), str(identity)),
                )
            return self._ensure_program_decision_in_transaction(
                identity, event_kind=kind, event_key=event_key, payload=payload or {}
            )

    def _program_decision_status_from_row(self, row: sqlite3.Row) -> ProgramControllerDecisionStatus:
        if row["program_id"] is None or row["event_kind"] is None or row["event_key"] is None:
            raise CorruptSchemaError("dispatch decision cannot be projected as a program decision")
        try:
            payload = strict_json_loads(str(row["summary_json"]), max_bytes=16_384)
        except ValueError as exc:
            raise CorruptSchemaError("program decision summary is invalid") from exc
        if not isinstance(payload, dict):
            raise CorruptSchemaError("program decision summary is not an object")
        try:
            return ProgramControllerDecisionStatus(
                ControllerDecisionId(str(row["decision_id"])),
                ProgramId(str(row["program_id"])),
                ProgramEventKind(str(row["event_kind"])),
                str(row["event_key"]),
                ControllerDecisionState(str(row["state"])),
                int(row["revision"]),
                Generation(int(row["current_generation"])),
                int(row["generation_budget"]),
                int(row["generation_used"]),
                ControllerClaimantKind(str(row["claimant_kind"])) if row["claimant_kind"] is not None else None,
                str(row["claimant_id"]) if row["claimant_id"] is not None else None,
                str(row["claim_lease_expires_at"]) if row["claim_lease_expires_at"] is not None else None,
                str(row["action_id"]) if row["action_id"] is not None else None,
                str(row["action_bundle_sha256"]) if row["action_bundle_sha256"] is not None else None,
                str(row["deadline"]),
                payload,
            )
        except (TypeError, ValueError) as exc:
            raise CorruptSchemaError("program decision projection is malformed") from exc

    def program_controller_decision(self, decision_id: ControllerDecisionId | str) -> ProgramControllerDecisionStatus:
        identity = ControllerDecisionId(str(decision_id))
        row = (
            self._db()
            .execute(
                "SELECT * FROM controller_decisions WHERE decision_id = ? AND program_id IS NOT NULL", (str(identity),)
            )
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"program controller decision does not exist: {identity}")
        return self._program_decision_status_from_row(row)

    def program_controller_decisions(self) -> tuple[ProgramControllerDecisionStatus, ...]:
        rows = (
            self._db()
            .execute(
                "SELECT * FROM controller_decisions WHERE program_id IS NOT NULL "
                "ORDER BY program_revision DESC, deadline, decision_id"
            )
            .fetchall()
        )
        return tuple(self._program_decision_status_from_row(row) for row in rows)

    def reconcile_stale_program_controller_decisions(self, *, now: str | None = None) -> int:
        """Supersede old program revisions and emit one fresh coalesced event."""

        current = now or utc_now()
        with self._transaction():
            stale = (
                self._db()
                .execute(
                    "SELECT d.*, r.program_revision AS current_program_revision, r.program_state "
                    "FROM controller_decisions d JOIN runs r ON r.run_id = d.program_id "
                    "WHERE d.program_id IS NOT NULL AND d.program_revision < r.program_revision "
                    "AND d.state IN ('awaiting_claim', 'claimed', 'human_attention_required') "
                    "AND d.action_id IS NULL ORDER BY d.program_id, d.decision_id"
                )
                .fetchall()
            )
            by_program: dict[str, list[str]] = {}
            for row in stale:
                updated = self._db().execute(
                    "UPDATE controller_decisions SET state = 'superseded', superseded_at = ?, "
                    "claim_lease_expires_at = NULL, human_attention_reason = NULL, updated_at = ? "
                    "WHERE decision_id = ? AND revision = ? AND state IN ('awaiting_claim', 'claimed', 'human_attention_required')",
                    (current, current, str(row["decision_id"]), int(row["revision"])),
                )
                if updated.rowcount == 1:
                    by_program.setdefault(str(row["program_id"]), []).append(str(row["decision_id"]))
            for program_id, decision_ids in by_program.items():
                program = (
                    self._db()
                    .execute(
                        "SELECT program_revision, program_state FROM runs WHERE run_id = ?",
                        (program_id,),
                    )
                    .fetchone()
                )
                if program is None or str(program["program_state"]) in {
                    ProgramState.COMPLETED.value,
                    ProgramState.FAILED.value,
                }:
                    continue
                revision = int(program["program_revision"])
                self._ensure_program_decision_in_transaction(
                    ProgramId(program_id),
                    event_kind=ProgramEventKind.CHECKPOINT,
                    event_key=f"revision/{revision}/coalesced",
                    payload={
                        "summary": "stale program events coalesced after a revision advance",
                        "superseded_decision_ids": decision_ids[:128],
                        "ready_milestones": [
                            str(item) for item in self.program_ready_milestones(ProgramId(program_id))
                        ],
                    },
                )
            return len(stale)

    def claim_program_controller_decision(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        claimant_id: str,
        expected_revision: int,
        generation: int | None = None,
        claimant_kind: ControllerClaimantKind | str = ControllerClaimantKind.MODEL,
        lease_seconds: float = 300.0,
        now: str | None = None,
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(str(decision_id))
        if not isinstance(claimant_id, str) or not claimant_id.strip() or len(claimant_id.encode("utf-8")) > 256:
            raise ValueError("program controller claimant id is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("program controller claim revision is invalid")
        current = now or utc_now()
        with self._transaction():
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decisions WHERE decision_id = ? AND program_id IS NOT NULL",
                    (str(identity),),
                )
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"program controller decision does not exist: {identity}")
            program = (
                self._db()
                .execute(
                    "SELECT program_revision FROM runs WHERE run_id = ?",
                    (str(row["program_id"]),),
                )
                .fetchone()
            )
            if program is None or int(row["program_revision"]) != int(program["program_revision"]):
                raise StaleWriter("program controller decision belongs to a stale program revision")
            concurrent = (
                self._db()
                .execute(
                    "SELECT decision_id FROM controller_decisions WHERE program_id = ? AND program_revision = ? "
                    "AND state = 'claimed' AND decision_id != ? LIMIT 1",
                    (str(row["program_id"]), int(row["program_revision"]), str(identity)),
                )
                .fetchone()
            )
            if concurrent is not None:
                raise StaleWriter("another controller generation owns this program revision")
            if generation is not None and int(row["current_generation"]) != int(generation):
                raise StaleWriter("program controller generation is stale")
            if str(row["state"]) != ControllerDecisionState.AWAITING_CLAIM.value:
                raise StaleWriter("program controller decision is not claimable")
            if int(row["revision"]) != expected_revision:
                raise StaleWriter("program controller decision revision is stale")
            if str(row["deadline"]) <= current:
                raise StaleWriter("program controller decision deadline has expired")
            token = secrets.token_urlsafe(32)
            token_hash = self._controller_claim_token_hash(token)
            expires = self._expires_after(current, lease_seconds)
            kind = (
                claimant_kind
                if isinstance(claimant_kind, ControllerClaimantKind)
                else ControllerClaimantKind(claimant_kind)
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = 'claimed', revision = revision + 1, generation_used = generation_used + 1, claimant_kind = ?, claimant_id = ?, claim_token_sha256 = ?, claim_started_at = ?, claim_lease_expires_at = ?, updated_at = ? WHERE decision_id = ? AND revision = ? AND state = 'awaiting_claim'",
                (kind.value, claimant_id, token_hash, current, expires, current, str(identity), expected_revision),
            )
            updated = (
                self._db()
                .execute(
                    "SELECT revision, current_generation FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            assert updated is not None
            return ControllerDecisionClaim(
                identity,
                Generation(int(updated["current_generation"])),
                kind,
                claimant_id,
                int(updated["revision"]),
                expires,
                token,
            )

    def record_control_executor_result(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        candidate: CandidateRecord | None,
        terminal_status: str,
        dispatch_id: DispatchId | str,
        result_sha256: str | None = None,
        blocker: TypedBlocker | None = None,
    ) -> LifecycleRecord:
        """Retain one ordinary-control candidate without closing acceptance.

        The queue is the execution transport, while ``h4.lifecycle`` remains
        the acceptance authority.  This transaction binds the terminal queue
        fact, candidate lineage, and the nonterminal REVIEWING state together;
        replay therefore cannot expose a completed milestone with no
        acceptance owner.
        """

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        dispatch = DispatchId(str(dispatch_id))
        if dispatch.parts[0] != run or dispatch.parts[1] != milestone or dispatch.parts[2] != RoleId("executor"):
            raise StaleWriter("control executor result dispatch does not match its milestone")
        if terminal_status not in {"completed", "failed", "external_blocked", "needs_decision"}:
            raise ValueError("control executor terminal status is unsupported")
        if result_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", result_sha256) is None:
            raise ValueError("control executor result digest is invalid")
        if candidate is not None and not isinstance(candidate, CandidateRecord):
            raise TypeError("control candidate must be typed")
        if blocker is not None and not isinstance(blocker, TypedBlocker):
            raise TypeError("control blocker must be typed")
        candidate_sha = candidate.commit_sha if candidate is not None else None
        disposition = candidate.disposition if candidate is not None else CandidateDisposition.NO_CANDIDATE
        if (
            terminal_status == "completed"
            and candidate is not None
            and candidate.disposition is CandidateDisposition.VERIFIED_COMMIT
        ):
            normalized_blocker = blocker
        else:
            normalized_blocker = _normalize_terminal_blocker(terminal_status, blocker)
        terminal_fact: JsonObject = {
            "dispatch_id": str(dispatch),
            "terminal_status": terminal_status,
            "candidate_sha": candidate_sha,
            "candidate_disposition": disposition.value,
            "candidate_workspace_path": str(candidate.workspace_path) if candidate is not None else None,
            "candidate_workspace_head": candidate.workspace_head if candidate is not None else None,
            "candidate_workspace_digest": candidate.workspace_digest if candidate is not None else None,
            "candidate_dirty": candidate.dirty if candidate is not None else False,
            "candidate_reason": candidate.reason if candidate is not None else None,
            "result_sha256": result_sha256,
            "blocker": normalized_blocker.to_json() if normalized_blocker is not None else None,
        }
        with self._transaction():
            run_row = self._db().execute("SELECT program_digest FROM runs WHERE run_id = ?", (str(run),)).fetchone()
            milestone_row = (
                self._db()
                .execute(
                    "SELECT current_state FROM milestones WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if run_row is None or milestone_row is None:
                raise RecordNotFound(f"control execution does not exist: {run}/{milestone}")
            if run_row["program_digest"] is not None:
                raise StaleWriter("program executor results use the program lifecycle authority")
            execution_row = (
                self._db()
                .execute(
                    "SELECT workspace_path, status FROM executions WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if execution_row is not None and candidate is not None:
                if Path(str(execution_row["workspace_path"])).resolve() != candidate.workspace_path.resolve():
                    raise StaleWriter("control candidate workspace conflicts with its durable execution")
                if str(execution_row["status"]) == ExecutionStatus.COMPLETED.value:
                    try:
                        terminal_integrity = self.get_execution_integrity(run, milestone)
                    except KeyError as exc:
                        raise StaleWriter("completed control execution lacks terminal integrity authority") from exc
                    if (
                        terminal_integrity.workspace_terminal_head_sha is None
                        or candidate.commit_sha != terminal_integrity.workspace_terminal_head_sha
                        or candidate.dirty
                    ):
                        raise StaleWriter("control candidate conflicts with its durable terminal authority")
            facts = self.review_lifecycle(run, milestone)
            for fact in reversed(facts):
                if fact.kind != "candidate_recorded" or fact.data.get("dispatch_id") != str(dispatch):
                    continue
                if fact.data != terminal_fact:
                    raise StaleWriter("control executor terminal replay conflicts with its durable candidate")
                return fact

            existing = self._program_candidate_sha(run, milestone)
            predecessor_sha: str | None = None
            if existing is not None and dispatch.parts[3] > self._candidate_dispatch_generation(facts, existing):
                prior_generation = self._candidate_dispatch_generation(facts, existing)
                if dispatch.parts[3] != prior_generation + 1 or not any(
                    fact.kind == "repair_requested" and fact.data.get("candidate_sha") == existing for fact in facts
                ):
                    raise StaleWriter("control candidate successor is outside the one-repair lifecycle")
                if candidate_sha == existing or (candidate_sha is None and terminal_status == "completed"):
                    raise StaleWriter("control repair must create a distinct successor candidate")
                if candidate_sha is not None:
                    predecessor_sha = existing
            elif existing is not None and existing != candidate_sha:
                raise StaleWriter("control candidate successor is not a newer executor generation")
            current = WorkflowState(str(milestone_row["current_state"]))
            verified_candidate = candidate is not None and candidate.disposition is CandidateDisposition.VERIFIED_COMMIT
            if terminal_status == "completed" and verified_candidate:
                if current is WorkflowState.STARTING:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        WorkflowState.RUNNING,
                        expected_state=WorkflowState.STARTING,
                        reason=ReasonCode.EXECUTION_FAILURE,
                    )
                    current = WorkflowState.RUNNING
                if current is WorkflowState.RUNNING:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        WorkflowState.COMPLETED,
                        expected_state=WorkflowState.RUNNING,
                        reason=ReasonCode.TERMINAL_OUTCOME,
                    )
                    current = WorkflowState.COMPLETED
                if current is WorkflowState.COMPLETED:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        WorkflowState.REVIEWING,
                        expected_state=WorkflowState.COMPLETED,
                        reason=ReasonCode.TERMINAL_OUTCOME,
                    )
                elif current is WorkflowState.REPAIR_REQUIRED:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        WorkflowState.REVIEWING,
                        expected_state=WorkflowState.REPAIR_REQUIRED,
                        reason=ReasonCode.TERMINAL_OUTCOME,
                    )
                elif current is not WorkflowState.REVIEWING:
                    raise StaleWriter(f"control candidate requires a reviewing milestone, found {current.value}")
            else:
                target = (
                    WorkflowState.BLOCKED
                    if terminal_status == "external_blocked"
                    else WorkflowState.NEEDS_DECISION
                    if terminal_status == "needs_decision"
                    else WorkflowState.FAILED
                )
                if current is WorkflowState.STARTING:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        WorkflowState.RUNNING,
                        expected_state=WorkflowState.STARTING,
                        reason=ReasonCode.EXECUTION_FAILURE,
                    )
                    current = WorkflowState.RUNNING
                if current is WorkflowState.RUNNING:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        target,
                        expected_state=WorkflowState.RUNNING,
                        reason=(
                            ReasonCode.ENVIRONMENT_BLOCKED
                            if target is WorkflowState.BLOCKED
                            else ReasonCode.DECISION_REQUIRED
                            if target is WorkflowState.NEEDS_DECISION
                            else ReasonCode.EXECUTION_FAILURE
                        ),
                    )
                elif current is WorkflowState.REPAIR_REQUIRED:
                    self._transition_program_state_in_transaction(
                        run,
                        milestone,
                        target,
                        expected_state=WorkflowState.REPAIR_REQUIRED,
                        reason=(
                            ReasonCode.ENVIRONMENT_BLOCKED
                            if target is WorkflowState.BLOCKED
                            else ReasonCode.DECISION_REQUIRED
                            if target is WorkflowState.NEEDS_DECISION
                            else ReasonCode.EXECUTION_FAILURE
                        ),
                    )
                elif current is not target:
                    raise StaleWriter(f"control executor result conflicts with milestone state {current.value}")

            fact = self._record_lifecycle_fact_in_transaction(
                run,
                milestone,
                phase=LifecyclePhase.EXECUTION,
                kind="candidate_recorded",
                data=terminal_fact,
            )
            if predecessor_sha is not None:
                self._record_lifecycle_fact_in_transaction(
                    run,
                    milestone,
                    phase=LifecyclePhase.REPAIR,
                    kind="candidate_superseded",
                    data={
                        "predecessor_sha": predecessor_sha,
                        "candidate_sha": candidate_sha,
                        "dispatch_id": str(dispatch),
                    },
                )
                self._record_lifecycle_fact_in_transaction(
                    run,
                    milestone,
                    phase=LifecyclePhase.REVIEW,
                    kind="reviews_invalidated",
                    data={"predecessor_sha": predecessor_sha, "candidate_sha": candidate_sha},
                )
            return fact

    def record_control_review(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        candidate_sha: str,
        dispatch_id: DispatchId | str,
        generation: int,
        result: object,
    ) -> LifecycleRecord:
        """Retain one exact ordinary-control authority review idempotently."""

        from .domain import ReviewResult

        if not isinstance(result, ReviewResult):
            raise TypeError("control review must be a typed ReviewResult")
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        dispatch = DispatchId(str(dispatch_id))
        if dispatch.parts[0] != run or dispatch.parts[1] != milestone or dispatch.parts[2] != result.reviewer_role:
            raise StaleWriter("control review dispatch does not match its reviewer authority")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("control review generation is invalid")
        if int(dispatch.parts[3]) != generation:
            raise StaleWriter("control review generation does not match its dispatch identity")
        if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
            raise ValueError("control review candidate is invalid")
        if result.reviewed_revision != candidate_sha or not result.fresh or not result.read_only:
            raise StaleWriter("control review does not cover the exact current candidate")
        expected_mode = {
            RoleId("code-reviewer"): AcceptanceMode.OBJECTIVE,
            RoleId("visual-reviewer"): AcceptanceMode.VISUAL,
            RoleId("architecture-reviewer"): AcceptanceMode.ARCHITECTURE,
        }.get(result.reviewer_role)
        if expected_mode is None or result.acceptance_mode is not expected_mode:
            raise StaleWriter("control review acceptance mode does not match its reviewer authority")
        data: JsonObject = {
            "review_id": result.review_id,
            "reviewer_role": str(result.reviewer_role),
            "acceptance_mode": result.acceptance_mode.value,
            "accepted": result.accepted,
            "candidate_sha": candidate_sha,
            "dispatch_id": str(dispatch),
            "generation": generation,
            "prior_review_id": result.prior_review_id,
            "finding_ids": [item.finding_id for item in result.findings],
            "finding_severities": {item.finding_id: item.severity.value for item in result.findings},
            "promotion_blocking": (not result.accepted) or any(item.promotion_blocking for item in result.findings),
            "review": review_result_to_json(result),
        }
        with self._transaction():
            if self._program_candidate_sha(run, milestone) != candidate_sha:
                raise StaleWriter("control review candidate is not the current candidate")
            for fact in reversed(self.review_lifecycle(run, milestone)):
                if fact.kind == "review_completed" and fact.data.get("review_id") == result.review_id:
                    if fact.data != data:
                        raise StaleWriter("control review replay conflicts with its durable result")
                    return fact
                if (
                    fact.kind == "review_completed"
                    and fact.data.get("candidate_sha") == candidate_sha
                    and fact.data.get("generation") == generation
                    and fact.data.get("reviewer_role") == str(result.reviewer_role)
                ):
                    raise StaleWriter("control authority already reviewed this exact candidate generation")
            return self._record_lifecycle_fact_in_transaction(
                run,
                milestone,
                phase=LifecyclePhase.REVIEW,
                kind="review_completed",
                data=data,
            )

    def record_program_candidate(
        self, program_id: ProgramId | str, milestone_id: MilestoneId | str, candidate_sha: str
    ) -> LifecycleRecord:
        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
            raise ValueError("program candidate commit is invalid")
        with self._transaction():
            existing = self._program_candidate_sha(program, milestone)
            if existing == candidate_sha:
                for fact in reversed(self.review_lifecycle(program, milestone)):
                    if fact.kind == "candidate_recorded" and fact.data.get("candidate_sha") == candidate_sha:
                        return fact
            fact = self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.EXECUTION,
                kind="candidate_recorded",
                data={"candidate_sha": candidate_sha},
            )
            self._ensure_program_decision_in_transaction(
                program,
                event_kind=ProgramEventKind.IMPLEMENTATION_COMPLETED,
                event_key=f"milestone/{milestone}/candidate/{candidate_sha}",
                payload={"milestone_id": str(milestone), "candidate_sha": candidate_sha},
            )
            return fact

    def adopt_program_candidate(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId | str,
        candidate: CandidateRecord,
        *,
        dispatch_id: DispatchId | str | None = None,
        terminal_status: str | None = None,
        blocker: TypedBlocker | None = None,
    ) -> ProgramStatus:
        """Register one independently verified existing candidate for review."""

        if (
            not isinstance(candidate, CandidateRecord)
            or candidate.disposition is not CandidateDisposition.VERIFIED_COMMIT
        ):
            raise ValueError("program adoption requires a verified commit candidate")
        if terminal_status is not None and terminal_status not in {
            "completed",
            "failed",
            "external_blocked",
            "needs_decision",
        }:
            raise ValueError("program adoption terminal status is unsupported")
        if blocker is not None and not isinstance(blocker, TypedBlocker):
            raise TypeError("program adoption blocker must be typed")
        assert candidate.commit_sha is not None
        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        dispatch_value = str(dispatch_id) if dispatch_id is not None else None
        with self._transaction():
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            milestone_row = (
                self._db()
                .execute(
                    "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                    (str(program), str(milestone)),
                )
                .fetchone()
            )
            if row is None or row["program_digest"] is None or milestone_row is None:
                raise RecordNotFound(f"program candidate does not exist: {program}/{milestone}")
            facts = self.review_lifecycle(program, milestone)
            inherited_status = terminal_status
            if inherited_status is None:
                for fact in reversed(facts):
                    if fact.kind != "executor_terminal":
                        continue
                    value = fact.data.get("terminal_status")
                    if isinstance(value, str) and value in {
                        "completed",
                        "failed",
                        "external_blocked",
                        "needs_decision",
                    }:
                        inherited_status = value
                        break
            if inherited_status is not None:
                blocker = _normalize_terminal_blocker(inherited_status, blocker)
            existing = self._program_candidate_sha(program, milestone)
            if any(
                item.kind in {"promotion_accepted", "integration_completed"}
                and item.data.get("candidate_sha") == candidate.commit_sha
                for item in facts
            ):
                raise StaleWriter("program candidate is already promoted or integrated")
            current = WorkflowState(str(milestone_row["current_state"]))
            predecessor_sha: str | None = None
            if existing is not None and existing != candidate.commit_sha:
                if (
                    current
                    not in {
                        WorkflowState.BLOCKED,
                        WorkflowState.NEEDS_DECISION,
                        WorkflowState.REPAIR_REQUIRED,
                    }
                    or dispatch_value is None
                ):
                    raise StaleWriter("program candidate commit changed before adoption")
                try:
                    dispatch = DispatchId(dispatch_value)
                except ValueError as exc:
                    raise StaleWriter("program candidate adoption dispatch identity is invalid") from exc
                prior_generation = self._candidate_dispatch_generation(facts, existing)
                if dispatch.parts[2] != RoleId("executor") or int(dispatch.parts[3]) <= prior_generation:
                    raise StaleWriter("program candidate adoption is not a successor generation")
                predecessor_sha = existing
            target_state = (
                WorkflowState.BLOCKED
                if inherited_status == "external_blocked"
                or (
                    blocker is not None
                    and blocker.kind is BlockerKind.EXTERNAL
                    and blocker.scope is BlockerScope.CURRENT_PROMOTION
                )
                else WorkflowState.REPAIR_REQUIRED
            )
            if (
                current
                in {
                    WorkflowState.FAILED,
                    WorkflowState.BLOCKED,
                    WorkflowState.NEEDS_DECISION,
                    WorkflowState.REPAIR_REQUIRED,
                }
                and current is not target_state
            ):
                self._transition_program_state_in_transaction(
                    program,
                    milestone,
                    target_state,
                    expected_state=current,
                    reason=ReasonCode.TERMINAL_OUTCOME,
                )
            self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.RECOVERY,
                kind="candidate_adopted",
                data={
                    "candidate_sha": candidate.commit_sha,
                    "candidate_disposition": candidate.disposition.value,
                    "candidate_workspace_path": str(candidate.workspace_path),
                    "candidate_workspace_head": candidate.workspace_head,
                    "candidate_workspace_digest": candidate.workspace_digest,
                    "candidate_dirty": candidate.dirty,
                    "candidate_reason": candidate.reason,
                    "dispatch_id": dispatch_value,
                    "terminal_status": inherited_status,
                    "blocker": blocker.to_json() if blocker is not None else None,
                },
            )
            if predecessor_sha is not None:
                self._record_program_fact_in_transaction(
                    program,
                    milestone,
                    phase=LifecyclePhase.REPAIR,
                    kind="candidate_superseded",
                    data={
                        "predecessor_sha": predecessor_sha,
                        "candidate_sha": candidate.commit_sha,
                        "dispatch_id": dispatch_value,
                    },
                )
            self._ensure_program_decision_in_transaction(
                program,
                event_kind=ProgramEventKind.IMPLEMENTATION_COMPLETED,
                event_key=f"milestone/{milestone}/candidate/{candidate.commit_sha}",
                payload={"milestone_id": str(milestone), "candidate_sha": candidate.commit_sha, "adopted": True},
            )
            refreshed = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            assert refreshed is not None
            return self._program_status_from_row(refreshed)

    def record_program_review(
        self, program_id: ProgramId | str, milestone_id: MilestoneId | str, result: object
    ) -> LifecycleRecord:
        from .domain import ReviewResult

        if not isinstance(result, ReviewResult):
            raise TypeError("program review must be a typed ReviewResult")
        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        candidate = self._program_candidate_sha(program, milestone)
        if candidate is None or result.reviewed_revision != candidate or not result.fresh or not result.read_only:
            raise StaleWriter("review does not cover the exact current candidate")
        with self._transaction():
            for fact in reversed(self.review_lifecycle(program, milestone)):
                if fact.kind == "review_completed" and fact.data.get("review_id") == result.review_id:
                    return fact
            data = {
                "review_id": result.review_id,
                "reviewer_role": str(result.reviewer_role),
                "acceptance_mode": result.acceptance_mode.value,
                "accepted": result.accepted,
                "candidate_sha": candidate,
                "finding_ids": [item.finding_id for item in result.findings],
                "promotion_blocking": (not result.accepted) or any(item.promotion_blocking for item in result.findings),
            }
            fact = self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.REVIEW,
                kind="review_completed",
                data=data,
            )
            graph_value = strict_json_loads(
                str(
                    self._db()
                    .execute("SELECT program_graph_json FROM runs WHERE run_id = ?", (str(program),))
                    .fetchone()[0]
                ),
                max_bytes=2_000_000,
            )
            if not isinstance(graph_value, dict):
                raise CorruptSchemaError("program graph projection is invalid")
            nodes = self._program_node_payloads(graph_value)
            capsule = nodes.get(str(milestone), {}).get("capsule")
            raw_modes = capsule.get("acceptance_modes", []) if isinstance(capsule, dict) else []
            role_by_mode = {
                "objective": "code-reviewer",
                "visual": "visual-reviewer",
                "architecture": "architecture-reviewer",
            }
            expected_roles = {
                role_by_mode[mode] for mode in raw_modes if isinstance(mode, str) and mode in role_by_mode
            }
            completed = {
                str(item.data.get("reviewer_role"))
                for item in self.review_lifecycle(program, milestone)
                if item.kind == "review_completed" and item.data.get("candidate_sha") == candidate
            }
            if expected_roles and expected_roles.issubset(completed):
                review_ids = sorted(
                    str(item.data["review_id"])
                    for item in self.review_lifecycle(program, milestone)
                    if item.kind == "review_completed" and item.data.get("candidate_sha") == candidate
                )
                review_digest = hashlib.sha256(_encode_json(review_ids).encode("utf-8")).hexdigest()[:16]
                blockers = any(
                    item.kind == "review_completed"
                    and item.data.get("candidate_sha") == candidate
                    and item.data.get("promotion_blocking") is True
                    for item in self.review_lifecycle(program, milestone)
                )
                self._ensure_program_decision_in_transaction(
                    program,
                    event_kind=ProgramEventKind.REVIEW_COMPLETED,
                    event_key=f"milestone/{milestone}/reviews/{review_digest}",
                    payload={
                        "milestone_id": str(milestone),
                        "candidate_sha": candidate,
                        "review_ids": review_ids,
                        "promotion_blocking": blockers,
                    },
                )
            return fact

    def reconcile_program_candidate(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId | str,
        candidate_sha: str,
        *,
        terminal_status: str,
        blocker: TypedBlocker,
        dispatch_id: DispatchId | str | None = None,
    ) -> ProgramStatus:
        """Repair an adopted candidate's missing terminal blocker idempotently."""

        if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
            raise ValueError("program candidate commit is invalid")
        if terminal_status not in {"failed", "external_blocked", "needs_decision"}:
            raise ValueError("candidate reconciliation terminal status is unsupported")
        if not isinstance(blocker, TypedBlocker):
            raise TypeError("candidate reconciliation blocker must be typed")
        if terminal_status == "external_blocked" and (
            blocker.kind is not BlockerKind.EXTERNAL or blocker.scope is not BlockerScope.CURRENT_PROMOTION
        ):
            raise ValueError("external candidate reconciliation requires a current-promotion external blocker")
        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        dispatch_value = str(dispatch_id) if dispatch_id is not None else None
        data = {
            "candidate_sha": candidate_sha,
            "terminal_status": terminal_status,
            "blocker": blocker.to_json(),
            "dispatch_id": dispatch_value,
        }
        with self._transaction():
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            milestone_row = (
                self._db()
                .execute(
                    "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                    (str(program), str(milestone)),
                )
                .fetchone()
            )
            if row is None or row["program_digest"] is None or milestone_row is None:
                raise RecordNotFound(f"program candidate does not exist: {program}/{milestone}")
            facts = self.review_lifecycle(program, milestone)
            if not any(
                fact.kind in {"candidate_adopted", "candidate_recorded", "candidate_reconciled"}
                and fact.data.get("candidate_sha") == candidate_sha
                for fact in facts
            ):
                raise RecordNotFound(f"program candidate is not adopted: {program}/{milestone}/{candidate_sha}")
            for fact in reversed(facts):
                if fact.kind != "candidate_reconciled" or fact.data.get("candidate_sha") != candidate_sha:
                    continue
                if fact.data != data:
                    raise StaleWriter("candidate reconciliation conflicts with its durable blocker")
                return self._program_status_from_row(row)
            current = WorkflowState(str(milestone_row["current_state"]))
            target = WorkflowState.BLOCKED if terminal_status == "external_blocked" else WorkflowState.REPAIR_REQUIRED
            if current is not target:
                self._transition_program_state_in_transaction(
                    program,
                    milestone,
                    target,
                    expected_state=current,
                    reason=ReasonCode.TERMINAL_OUTCOME,
                )
            self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.RECOVERY,
                kind="candidate_reconciled",
                data=data,
            )
            self._ensure_program_decision_in_transaction(
                program,
                event_kind=ProgramEventKind.CONTROLLER_ATTENTION,
                event_key=f"milestone/{milestone}/candidate/{candidate_sha}/reconciled",
                payload={
                    "milestone_id": str(milestone),
                    "candidate_sha": candidate_sha,
                    "terminal_status": terminal_status,
                    "blocker": blocker.to_json(),
                },
            )
            refreshed = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            assert refreshed is not None
            return self._program_status_from_row(refreshed)

    def _program_candidate_sha(self, program_id: ProgramId | str, milestone_id: MilestoneId | str) -> str | None:
        facts = self.review_lifecycle(program_id, milestone_id)
        for fact in reversed(facts):
            value = fact.data.get("candidate_sha")
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _candidate_dispatch_generation(facts: Sequence[LifecycleRecord], candidate_sha: str) -> int:
        """Return the greatest executor generation that recorded one candidate."""

        generation = 0
        for fact in facts:
            if fact.data.get("candidate_sha") != candidate_sha:
                continue
            dispatch_value = fact.data.get("dispatch_id")
            if not isinstance(dispatch_value, str):
                continue
            try:
                dispatch = DispatchId(dispatch_value)
            except ValueError:
                continue
            if dispatch.parts[2] == RoleId("executor"):
                generation = max(generation, int(dispatch.parts[3]))
        return generation

    def _program_candidate_blocker(
        self, program_id: ProgramId | str, milestone_id: MilestoneId | str, candidate_sha: str
    ) -> TypedBlocker | None:
        """Return the current unresolved terminal blocker for one candidate."""

        blocker: TypedBlocker | None = None
        for fact in self.review_lifecycle(program_id, milestone_id):
            data = fact.data
            if data.get("candidate_sha") != candidate_sha:
                continue
            raw = data.get("blocker")
            if raw is not None:
                if not isinstance(raw, Mapping):
                    raise CorruptSchemaError("program blocker projection is invalid")
                try:
                    blocker = TypedBlocker.from_json(raw)
                except (TypeError, ValueError) as exc:
                    raise CorruptSchemaError("program blocker projection is invalid") from exc
            if fact.kind == "candidate_blocker_resolved":
                gate_id = data.get("blocker_gate_id")
                resolution = data.get("blocker_resolution")
                if (
                    not isinstance(gate_id, str)
                    or resolution not in {"resolve", "supersede"}
                    or blocker is None
                    or blocker.gate_id != gate_id
                ):
                    raise CorruptSchemaError("program blocker resolution is stale or malformed")
                blocker = None
        return blocker

    def _program_applicable_blocker(
        self,
        program_id: ProgramId | str,
        *,
        operation: str,
        target_milestone_id: MilestoneId | str,
    ) -> TypedBlocker | None:
        """Return the first current blocker governing one program operation."""

        status = self.program_status(program_id)
        target_id = MilestoneId(str(target_milestone_id))
        target = next((node for node in status.nodes if node.milestone_id == target_id), None)
        if target is None:
            raise RecordNotFound(f"program milestone does not exist: {program_id}/{target_id}")
        for source in status.nodes:
            blocker = source.blocker
            if blocker is not None and blocker_applies_to(
                blocker,
                operation=operation,
                source_milestone_id=source.milestone_id,
                target_milestone_id=target_id,
                target_dependencies=target.dependencies,
            ):
                return blocker
        return None

    def _transition_program_state_in_transaction(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId,
        target: WorkflowState,
        *,
        expected_state: WorkflowState,
        reason: ReasonCode,
    ) -> None:
        """Advance a program milestone while its controller action is open."""

        current = (
            self._db()
            .execute(
                "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                (str(program_id), str(milestone_id)),
            )
            .fetchone()
        )
        if current is None:
            raise RecordNotFound(f"program milestone does not exist: {program_id}/{milestone_id}")
        actual = WorkflowState(str(current["current_state"]))
        if actual is expected_state and actual is not target:
            if not is_transition_allowed(actual, target):
                raise InvalidTransition(f"{actual.value} -> {target.value} is not allowed")
            now = utc_now()
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(program_id), str(milestone_id)),
            )
            self._append_event_in_transaction(
                RunId(str(program_id)),
                milestone_id,
                from_state=actual,
                to_state=target,
                event_type="state_transition",
                reason=WorkflowReason(reason),
                dispatch_id=None,
                data=None,
            )
            return
        if actual is not target:
            raise StaleWriter(
                f"program milestone state is stale: expected {expected_state.value}, current {actual.value}"
            )

    def record_program_executor_result(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId | str,
        *,
        candidate_sha: str | None,
        terminal_status: str,
        dispatch_id: DispatchId | str | None = None,
        result_sha256: str | None = None,
        candidate_record: CandidateRecord | None = None,
        blocker: TypedBlocker | None = None,
    ) -> ProgramStatus:
        """Close one executor result while retaining any verified workspace fact."""

        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        if candidate_sha is not None and re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
            raise ValueError("program candidate commit is invalid")
        if terminal_status not in {"completed", "failed", "external_blocked", "needs_decision"}:
            raise ValueError("program executor terminal status is unsupported")
        if result_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", result_sha256) is None:
            raise ValueError("program executor result digest is invalid")
        if candidate_record is not None and not isinstance(candidate_record, CandidateRecord):
            raise TypeError("program candidate record must be typed")
        if blocker is not None and not isinstance(blocker, TypedBlocker):
            raise TypeError("program blocker must be typed")
        if candidate_record is not None:
            record_sha = candidate_record.commit_sha
            if record_sha != candidate_sha:
                raise StaleWriter("program candidate record does not match candidate commit")
            candidate_disposition = candidate_record.disposition
        elif candidate_sha is not None:
            candidate_disposition = CandidateDisposition.VERIFIED_COMMIT
        else:
            candidate_disposition = CandidateDisposition.NO_CANDIDATE
        blocker = _normalize_terminal_blocker(terminal_status, blocker)
        dispatch_value = str(dispatch_id) if dispatch_id is not None else None
        terminal_fact = {
            "dispatch_id": dispatch_value,
            "terminal_status": terminal_status,
            "candidate_sha": candidate_sha,
            "candidate_disposition": candidate_disposition.value,
            "candidate_workspace_path": str(candidate_record.workspace_path) if candidate_record is not None else None,
            "candidate_workspace_head": candidate_record.workspace_head if candidate_record is not None else None,
            "candidate_workspace_digest": candidate_record.workspace_digest if candidate_record is not None else None,
            "candidate_dirty": candidate_record.dirty if candidate_record is not None else False,
            "candidate_reason": candidate_record.reason if candidate_record is not None else None,
            "result_sha256": result_sha256,
            "blocker": blocker.to_json() if blocker is not None else None,
        }
        with self._transaction():
            row = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            milestone_row = (
                self._db()
                .execute(
                    "SELECT * FROM milestones WHERE run_id = ? AND milestone_id = ?",
                    (str(program), str(milestone)),
                )
                .fetchone()
            )
            if row is None or row["program_digest"] is None or milestone_row is None:
                raise RecordNotFound(f"program executor result does not exist: {program}/{milestone}")
            for fact in reversed(self.review_lifecycle(program, milestone)):
                if fact.kind != "executor_terminal" or fact.data.get("dispatch_id") != dispatch_value:
                    continue
                if fact.data != terminal_fact:
                    raise StaleWriter("program executor terminal replay conflicts with its durable result")
                return self._program_status_from_row(row)
            current = WorkflowState(str(milestone_row["current_state"]))
            candidate_bearing = candidate_sha is not None
            if terminal_status == "completed" and candidate_bearing:
                target_state = WorkflowState.COMPLETED
            elif candidate_bearing and terminal_status == "external_blocked":
                target_state = WorkflowState.BLOCKED
            elif candidate_bearing and terminal_status == "needs_decision":
                target_state = WorkflowState.NEEDS_DECISION
            elif candidate_bearing and terminal_status == "failed":
                target_state = WorkflowState.REPAIR_REQUIRED
            else:
                target_state = WorkflowState.FAILED
            if current is WorkflowState.STARTING and target_state in {
                WorkflowState.COMPLETED,
                WorkflowState.NEEDS_DECISION,
                WorkflowState.REPAIR_REQUIRED,
            }:
                now = utc_now()
                self._db().execute(
                    "UPDATE milestones SET current_state = 'RUNNING', updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                    (now, str(program), str(milestone)),
                )
                self._append_event_in_transaction(
                    RunId(str(program)),
                    milestone,
                    from_state=WorkflowState.STARTING,
                    to_state=WorkflowState.RUNNING,
                    event_type="state_transition",
                    reason=WorkflowReason(ReasonCode.EXECUTION_FAILURE),
                    dispatch_id=None,
                    data=None,
                )
                current = WorkflowState.RUNNING
            if current in {WorkflowState.STARTING, WorkflowState.RUNNING}:
                now = utc_now()
                self._db().execute(
                    "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                    (target_state.value, now, str(program), str(milestone)),
                )
                self._append_event_in_transaction(
                    RunId(str(program)),
                    milestone,
                    from_state=current,
                    to_state=target_state,
                    event_type="state_transition",
                    reason=WorkflowReason(
                        ReasonCode.TERMINAL_OUTCOME
                        if target_state is WorkflowState.COMPLETED
                        else ReasonCode.EXECUTION_FAILURE
                    ),
                    dispatch_id=None,
                    data=None,
                )
            elif current is WorkflowState.PLANNED and target_state is WorkflowState.FAILED:
                # Low-level callers may close an unclaimed synthetic failure;
                # retain its terminal fact without inventing a dispatch state.
                pass
            elif current is not target_state and target_state is not WorkflowState.COMPLETED:
                raise StaleWriter(f"program executor result requires a launchable milestone, found {current.value}")
            existing = self._program_candidate_sha(program, milestone)
            predecessor_sha: str | None = None
            if existing not in {None, candidate_sha}:
                if candidate_sha is None or current not in {
                    WorkflowState.STARTING,
                    WorkflowState.RUNNING,
                    WorkflowState.REPAIR_REQUIRED,
                    WorkflowState.BLOCKED,
                    WorkflowState.NEEDS_DECISION,
                }:
                    raise StaleWriter("program candidate commit changed after executor completion")
                if dispatch_value is None:
                    raise StaleWriter("program candidate successor requires a bound dispatch")
                try:
                    dispatch = DispatchId(dispatch_value)
                except ValueError as exc:
                    raise StaleWriter("program candidate successor dispatch identity is invalid") from exc
                prior_generation = self._candidate_dispatch_generation(
                    self.review_lifecycle(program, milestone), existing
                )
                if dispatch.parts[2] != RoleId("executor") or int(dispatch.parts[3]) <= prior_generation:
                    raise StaleWriter("program candidate successor is not a newer executor generation")
                predecessor_sha = existing
            self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.EXECUTION,
                kind="candidate_recorded",
                data=terminal_fact,
            )
            if predecessor_sha is not None:
                self._record_program_fact_in_transaction(
                    program,
                    milestone,
                    phase=LifecyclePhase.REPAIR,
                    kind="candidate_superseded",
                    data={
                        "predecessor_sha": predecessor_sha,
                        "candidate_sha": candidate_sha,
                        "dispatch_id": dispatch_value,
                    },
                )
            if target_state is WorkflowState.COMPLETED and candidate_sha is not None:
                self._ensure_program_decision_in_transaction(
                    program,
                    event_kind=ProgramEventKind.IMPLEMENTATION_COMPLETED,
                    event_key=f"milestone/{milestone}/candidate/{candidate_sha}",
                    payload={"milestone_id": str(milestone), "candidate_sha": candidate_sha},
                )
            else:
                if candidate_bearing:
                    program_state = ProgramState.RUNNING.value
                else:
                    program_state = (
                        ProgramState.NEEDS_DECISION.value
                        if terminal_status == "needs_decision"
                        else ProgramState.EXTERNAL_BLOCKED.value
                        if terminal_status == "external_blocked"
                        else ProgramState.FAILED.value
                    )
                self._db().execute(
                    "UPDATE runs SET program_state = ?, program_revision = program_revision + 1 WHERE run_id = ?",
                    (program_state, str(program)),
                )
                reason = f"executor/{milestone}/{dispatch_value or 'unbound'}/{terminal_status}"
                self._ensure_program_decision_in_transaction(
                    program,
                    event_kind=ProgramEventKind.CONTROLLER_ATTENTION,
                    event_key=reason,
                    payload={
                        "milestone_id": str(milestone),
                        "terminal_status": terminal_status,
                        "dispatch_id": dispatch_value,
                        "result_sha256": result_sha256,
                        "candidate_sha": candidate_sha,
                        "candidate_disposition": candidate_disposition.value,
                        "blocker": blocker.to_json() if blocker is not None else None,
                    },
                )
            self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.EXECUTION,
                kind="executor_terminal",
                data=terminal_fact,
            )
            refreshed = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            assert refreshed is not None
            return self._program_status_from_row(refreshed)

    def program_executor_terminal_recorded(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId | str,
        *,
        dispatch_id: DispatchId | str,
        terminal_status: str,
        result_sha256: str,
    ) -> bool:
        """Check exact queue-result projection before reading its workspace."""

        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        dispatch_value = str(DispatchId(str(dispatch_id)))
        if terminal_status not in {"completed", "failed", "external_blocked", "needs_decision"}:
            raise ValueError("program executor terminal status is unsupported")
        if re.fullmatch(r"[0-9a-f]{64}", result_sha256) is None:
            raise ValueError("program executor result digest is invalid")
        for fact in reversed(self.review_lifecycle(program, milestone)):
            if fact.kind != "executor_terminal" or fact.data.get("dispatch_id") != dispatch_value:
                continue
            if fact.data.get("terminal_status") != terminal_status or fact.data.get("result_sha256") != result_sha256:
                raise StaleWriter("program executor terminal replay conflicts with its durable result")
            return True
        return False

    def _program_action_effects(
        self,
        bundle: ModelFacingProgramControllerActionBundle,
        *,
        program: sqlite3.Row,
        now: str,
    ) -> list[str]:
        graph_value = strict_json_loads(str(program["program_graph_json"]), max_bytes=2_000_000)
        if not isinstance(graph_value, dict):
            raise CorruptSchemaError("program graph projection is invalid")
        nodes = self._program_node_payloads(graph_value)
        effects: list[str] = []
        role_by_mode = {
            "objective": "code-reviewer",
            "visual": "visual-reviewer",
            "architecture": "architecture-reviewer",
        }
        for action in bundle.actions:
            if action.kind is ProgramControllerActionKind.START_READY_MILESTONES:
                selected = tuple(action.milestone_ids)
                ready = {str(item) for item in self.program_status(bundle.program_id).ready_milestones}
                if not set(selected).issubset(ready):
                    raise StaleWriter("program start action names a non-ready milestone")
                seen: set[str] = set()
                for milestone_id in selected:
                    if milestone_id not in nodes:
                        raise StaleWriter("program start action names an unknown milestone")
                    if (
                        self._program_applicable_blocker(
                            bundle.program_id,
                            operation="start",
                            target_milestone_id=milestone_id,
                        )
                        is not None
                    ):
                        raise StaleWriter("program start action has a blocking program prerequisite")
                    surfaces = set(nodes[milestone_id].get("mutable_surfaces", []))
                    if seen & surfaces:
                        raise StaleWriter("program start action has overlapping mutable ownership")
                    seen.update(surfaces)
                    existing = (
                        self._db()
                        .execute(
                            "SELECT 1 FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = 'executor'",
                            (str(bundle.program_id), milestone_id),
                        )
                        .fetchone()
                    )
                    if existing is not None:
                        raise StaleWriter("program start action would duplicate an executor START")
                effects.append(f"start:{','.join(selected)}")
            elif action.kind is ProgramControllerActionKind.START_REVIEWS:
                if action.milestone_id not in nodes or action.candidate_sha != self._program_candidate_sha(
                    bundle.program_id, action.milestone_id
                ):
                    raise StaleWriter("program review action does not target the exact candidate")
                if (
                    self._program_applicable_blocker(
                        bundle.program_id,
                        operation="start",
                        target_milestone_id=action.milestone_id,
                    )
                    is not None
                ):
                    raise StaleWriter("program review action has a blocking program prerequisite")
                capsule = nodes[action.milestone_id].get("capsule")
                modes = capsule.get("acceptance_modes", []) if isinstance(capsule, dict) else []
                expected_roles = tuple(
                    sorted(role_by_mode[mode] for mode in modes if isinstance(mode, str) and mode in role_by_mode)
                )
                if tuple(action.review_roles) != expected_roles:
                    raise StaleWriter("program review action does not name every declared authority")
                for role in action.review_roles:
                    latest = (
                        self._db()
                        .execute(
                            "SELECT d.dispatch_id, q.state FROM dispatches d LEFT JOIN dispatch_queue q "
                            "ON q.dispatch_id = d.dispatch_id WHERE d.run_id = ? AND d.milestone_id = ? "
                            "AND d.role = ? ORDER BY d.generation DESC LIMIT 1",
                            (str(bundle.program_id), action.milestone_id, role),
                        )
                        .fetchone()
                    )
                    if latest is not None and (
                        latest["state"] is None or str(latest["state"]) not in {"completed", "failed", "cancelled"}
                    ):
                        raise StaleWriter("program review action would duplicate a current reviewer START")
                state = self.current_state(bundle.program_id, action.milestone_id)
                if state in {
                    WorkflowState.COMPLETED,
                    WorkflowState.BLOCKED,
                    WorkflowState.NEEDS_DECISION,
                    WorkflowState.REPAIR_REQUIRED,
                }:
                    self._transition_program_state_in_transaction(
                        bundle.program_id,
                        MilestoneId(action.milestone_id),
                        WorkflowState.REVIEWING,
                        expected_state=state,
                        reason=ReasonCode.TERMINAL_OUTCOME,
                    )
                elif state is not WorkflowState.REVIEWING:
                    raise StaleWriter("program review action requires a completed or reviewing milestone")
                effects.append(f"review:{action.milestone_id}")
            elif action.kind is ProgramControllerActionKind.REQUEST_REPAIR:
                if action.milestone_id not in nodes or action.candidate_sha != self._program_candidate_sha(
                    bundle.program_id, action.milestone_id
                ):
                    raise StaleWriter("program repair action does not target the exact candidate")
                if not action.finding_ids:
                    raise StaleWriter("program repair action has no concrete findings")
                facts = self.review_lifecycle(bundle.program_id, action.milestone_id)
                known_blockers = {
                    finding_id
                    for item in facts
                    if item.kind == "review_completed"
                    and item.data.get("candidate_sha") == action.candidate_sha
                    and item.data.get("promotion_blocking") is True
                    for finding_id in item.data.get("finding_ids", [])
                    if isinstance(finding_id, str)
                }
                if not set(action.finding_ids).issubset(known_blockers):
                    raise StaleWriter("program repair action names a non-blocking or unknown finding")
                if not any(
                    str(item[0]) == "executor"
                    for item in self._db()
                    .execute(
                        "SELECT role FROM dispatches WHERE run_id = ? AND milestone_id = ?",
                        (str(bundle.program_id), action.milestone_id),
                    )
                    .fetchall()
                ):
                    raise StaleWriter("program repair requires its original executor owner")
                state = self.current_state(bundle.program_id, action.milestone_id)
                if state is WorkflowState.REVIEWING:
                    self._transition_program_state_in_transaction(
                        bundle.program_id,
                        MilestoneId(action.milestone_id),
                        WorkflowState.REPAIR_REQUIRED,
                        expected_state=WorkflowState.REVIEWING,
                        reason=ReasonCode.REVIEW_REJECTED,
                    )
                elif state is not WorkflowState.REPAIR_REQUIRED:
                    raise StaleWriter("program repair requires a reviewing milestone")
                self._record_program_fact_in_transaction(
                    bundle.program_id,
                    action.milestone_id,
                    phase=LifecyclePhase.REPAIR,
                    kind="repair_requested",
                    data={
                        "candidate_sha": action.candidate_sha,
                        "finding_ids": list(action.finding_ids),
                        "same_owner": True,
                    },
                )
                effects.append(f"repair:{action.milestone_id}")
            elif action.kind is ProgramControllerActionKind.RESOLVE_CANDIDATE_BLOCKER:
                if (
                    action.milestone_id not in nodes
                    or action.candidate_sha != self._program_candidate_sha(bundle.program_id, action.milestone_id)
                    or action.blocker_gate_id is None
                    or action.blocker_resolution is None
                ):
                    raise StaleWriter("program blocker action does not target the exact candidate")
                if action.blocker_resolution not in {"resolve", "supersede"}:
                    raise StaleWriter("program blocker action has an unsupported resolution")
                blocker = self._program_candidate_blocker(bundle.program_id, action.milestone_id, action.candidate_sha)
                if blocker is None or blocker.gate_id != action.blocker_gate_id:
                    raise StaleWriter("program blocker action does not target the current terminal blocker")
                self._record_program_fact_in_transaction(
                    bundle.program_id,
                    action.milestone_id,
                    phase=LifecyclePhase.ACCEPTANCE,
                    kind="candidate_blocker_resolved",
                    data={
                        "candidate_sha": action.candidate_sha,
                        "blocker_gate_id": action.blocker_gate_id,
                        "blocker_resolution": action.blocker_resolution,
                    },
                )
                effects.append(f"resolve-blocker:{action.milestone_id}:{action.blocker_gate_id}")
            elif action.kind is ProgramControllerActionKind.PROMOTE_CANDIDATE:
                if action.milestone_id not in nodes or action.candidate_sha != self._program_candidate_sha(
                    bundle.program_id, action.milestone_id
                ):
                    raise StaleWriter("program promotion action does not target the exact candidate")
                facts = self.review_lifecycle(bundle.program_id, action.milestone_id)
                accepted = {
                    str(item.data.get("review_id"))
                    for item in facts
                    if item.kind == "review_completed"
                    and item.data.get("accepted") is True
                    and item.data.get("candidate_sha") == action.candidate_sha
                }
                capsule = nodes[action.milestone_id].get("capsule")
                modes = capsule.get("acceptance_modes", []) if isinstance(capsule, dict) else []
                expected_roles = {
                    role_by_mode[mode] for mode in modes if isinstance(mode, str) and mode in role_by_mode
                }
                accepted_roles = {
                    str(item.data.get("reviewer_role"))
                    for item in facts
                    if item.kind == "review_completed"
                    and item.data.get("accepted") is True
                    and item.data.get("candidate_sha") == action.candidate_sha
                }
                if set(action.review_ids) != accepted or not accepted or accepted_roles != expected_roles:
                    raise StaleWriter("program promotion requires every exact accepted review")
                if any(
                    item.kind == "review_completed"
                    and item.data.get("candidate_sha") == action.candidate_sha
                    and item.data.get("promotion_blocking") is True
                    for item in facts
                ):
                    raise StaleWriter("program promotion has a promotion-blocking finding")
                terminal_blocker = self._program_applicable_blocker(
                    bundle.program_id,
                    operation="promote",
                    target_milestone_id=action.milestone_id,
                )
                if terminal_blocker is not None:
                    raise StaleWriter("program promotion has a promotion-blocking terminal blocker")
                if not any(
                    item.kind == "promotion_accepted" and item.data.get("candidate_sha") == action.candidate_sha
                    for item in facts
                ):
                    self._record_program_fact_in_transaction(
                        bundle.program_id,
                        action.milestone_id,
                        phase=LifecyclePhase.ACCEPTANCE,
                        kind="promotion_accepted",
                        data={"candidate_sha": action.candidate_sha, "review_ids": list(action.review_ids)},
                    )
                state = self.current_state(bundle.program_id, action.milestone_id)
                if state is WorkflowState.REVIEWING:
                    self._transition_program_state_in_transaction(
                        bundle.program_id,
                        MilestoneId(action.milestone_id),
                        WorkflowState.ACCEPTED,
                        expected_state=WorkflowState.REVIEWING,
                        reason=ReasonCode.TERMINAL_OUTCOME,
                    )
                elif state is not WorkflowState.ACCEPTED:
                    raise StaleWriter("program promotion requires a reviewing milestone")
                effects.append(f"promote:{action.milestone_id}")
            elif action.kind is ProgramControllerActionKind.INTEGRATE_CANDIDATE:
                if action.milestone_id not in nodes or action.candidate_sha != self._program_candidate_sha(
                    bundle.program_id, action.milestone_id
                ):
                    raise StaleWriter("program integration action does not target the exact candidate")
                if action.expected_trunk_head != str(program["trunk_head"]):
                    raise StaleWriter("program integration trunk HEAD is stale")
                facts = self.review_lifecycle(bundle.program_id, action.milestone_id)
                if not any(
                    item.kind == "promotion_accepted" and item.data.get("candidate_sha") == action.candidate_sha
                    for item in facts
                ):
                    raise StaleWriter("program integration requires an exact promotion receipt")
                terminal_blocker = self._program_applicable_blocker(
                    bundle.program_id,
                    operation="integrate",
                    target_milestone_id=action.milestone_id,
                )
                if terminal_blocker is not None:
                    raise StaleWriter("program integration has a promotion-blocking terminal blocker")
                integration_id = f"integration/{bundle.program_id}/{action.milestone_id}/{action.candidate_sha}"
                existing = (
                    self._db()
                    .execute("SELECT state FROM integration_outbox WHERE integration_id = ?", (integration_id,))
                    .fetchone()
                )
                if existing is None:
                    self._db().execute(
                        "INSERT INTO integration_outbox(integration_id, program_id, milestone_id, candidate_sha, expected_trunk_head, strategy, state, receipt_json, receipt_sha256, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)",
                        (
                            integration_id,
                            str(bundle.program_id),
                            action.milestone_id,
                            action.candidate_sha,
                            action.expected_trunk_head,
                            action.integration_strategy,
                            now,
                        ),
                    )
                elif str(existing["state"]) != "pending":
                    raise StaleWriter("program integration is already terminal")
                effects.append(f"integrate:{action.milestone_id}")
            elif action.kind is ProgramControllerActionKind.REQUIRE_REPLAN:
                self._db().execute(
                    "UPDATE runs SET program_state = 'needs_decision' WHERE run_id = ?", (str(bundle.program_id),)
                )
                effects.append("require_replan")
            elif action.kind is ProgramControllerActionKind.REQUIRE_HUMAN_ATTENTION:
                self._db().execute(
                    "UPDATE runs SET program_state = 'needs_decision' WHERE run_id = ?", (str(bundle.program_id),)
                )
                effects.append("require_human_attention")
            else:
                effects.append("acknowledge_only")
        return effects

    def _record_program_fact_in_transaction(
        self,
        program_id: ProgramId,
        milestone_id: MilestoneId | str,
        *,
        phase: LifecyclePhase,
        kind: str,
        data: Mapping[str, object],
    ) -> LifecycleRecord:
        """Append one idempotent H4 fact while a program action is open."""

        return self._record_lifecycle_fact_in_transaction(
            program_id,
            milestone_id,
            phase=phase,
            kind=kind,
            data=data,
        )

    def _record_lifecycle_fact_in_transaction(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        phase: LifecyclePhase,
        kind: str,
        data: Mapping[str, object],
    ) -> LifecycleRecord:
        """Append one idempotent H4 fact for either lifecycle authority."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        for fact in self.review_lifecycle(run, milestone):
            if fact.kind == kind and fact.data == dict(data):
                return fact
        payload = {"schema": "codex-flow/h4/v1", "phase": phase.value, "kind": kind, "payload": dict(data)}
        _envelope, encoded = _encode_h4_event_data(payload)
        sequence = (
            int(
                self._db()
                .execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM h4.lifecycle WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()[0]
            )
            + 1
        )
        self._db().execute(
            "INSERT INTO h4.lifecycle(run_id, milestone_id, sequence, phase, kind, data_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(run), str(milestone), sequence, phase.value, kind, encoded, utc_now()),
        )
        return LifecycleRecord(phase, kind, sequence, dict(data))

    def submit_program_controller_actions(
        self,
        bundle: ModelFacingProgramControllerActionBundle,
        *,
        claimant_id: str,
        token: str,
        allow_recovered: bool = False,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        if not isinstance(bundle, ModelFacingProgramControllerActionBundle):
            raise TypeError("program action bundle must be typed")
        current = now or utc_now()
        token_hash = self._controller_claim_token_hash(token) if token else None
        if token_hash is None and not allow_recovered:
            raise ValueError("program controller claim token is required")
        bundle_json = bundle.to_json_bytes().decode("utf-8")
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decisions WHERE decision_id = ? AND program_id IS NOT NULL",
                    (str(bundle.decision_id),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"program controller decision does not exist: {bundle.decision_id}")
            program = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(bundle.program_id),)).fetchone()
            if program is None:
                raise RecordNotFound(f"program does not exist: {bundle.program_id}")
            existing_outbox = (
                self._db()
                .execute("SELECT * FROM controller_action_outbox WHERE decision_id = ?", (str(bundle.decision_id),))
                .fetchone()
            )
            if existing_outbox is not None:
                if str(existing_outbox["bundle_sha256"]) != bundle.sha256:
                    raise StaleWriter("program controller action is already committed with different bytes")
                if not allow_recovered and str(existing_outbox["claimant_id"]) != claimant_id:
                    raise StaleWriter("program controller action replay claimant is stale")
                return ControllerActionReceipt(
                    str(existing_outbox["action_id"]),
                    bundle.decision_id,
                    Generation(int(existing_outbox["generation"])),
                    int(existing_outbox["expected_revision"]),
                    str(existing_outbox["bundle_sha256"]),
                    cast(JsonObject, strict_json_loads(str(existing_outbox["effect_receipt_json"]))),
                    str(existing_outbox["state"]),
                    str(existing_outbox["committed_at"]),
                    str(existing_outbox["acknowledged_at"]) if existing_outbox["acknowledged_at"] else None,
                )
            if not allow_recovered and (
                str(decision["claim_token_sha256"]) != token_hash or str(decision["claimant_id"]) != claimant_id
            ):
                raise StaleWriter("program controller claim is stale")
            if allow_recovered:
                if str(decision["state"]) not in {
                    ControllerDecisionState.CLAIMED.value,
                    ControllerDecisionState.AWAITING_CLAIM.value,
                }:
                    raise StaleWriter("program controller recovery decision is stale")
                if decision["claimant_kind"] != ControllerClaimantKind.MODEL.value:
                    raise StaleWriter("program controller recovery claimant is not the model owner")
            elif str(decision["state"]) != ControllerDecisionState.CLAIMED.value:
                raise StaleWriter("program controller decision revision is stale")
            if int(decision["current_generation"]) != int(bundle.generation):
                raise StaleWriter("program controller generation is stale")
            if (
                int(decision["program_revision"]) != bundle.expected_program_revision
                or int(program["program_revision"]) != bundle.expected_program_revision
            ):
                raise StaleWriter("program revision is stale")
            if str(program["program_digest"]) != bundle.plan_digest:
                raise StaleWriter("program plan digest is stale")
            if str(program["trunk_head"]) != bundle.expected_trunk_head:
                raise StaleWriter("program trunk HEAD is stale")
            if bundle.event_kind.value != str(decision["event_kind"]) or bundle.event_key != str(decision["event_key"]):
                raise StaleWriter("program controller event identity is stale")
            effects = self._program_action_effects(bundle, program=program, now=current)
            decision_revision = int(decision["revision"])
            revision = int(program["program_revision"]) + 1
            self._db().execute(
                "UPDATE runs SET program_revision = ?, program_state = CASE WHEN program_state = 'registered' THEN 'running' ELSE program_state END WHERE run_id = ?",
                (revision, str(bundle.program_id)),
            )
            receipt_payload: JsonObject = {
                "action_id": bundle.action_id,
                "decision_id": str(bundle.decision_id),
                "generation": int(bundle.generation),
                "bundle_sha256": bundle.sha256,
                "applied_actions": effects,
                "program_revision": revision,
                "external_effect_ids": [effect_id for effect_id, _milestone_id in bundle.external_effects],
            }
            receipt_json = _encode_json(receipt_payload)
            receipt_digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            self._db().execute(
                "INSERT INTO controller_action_outbox(action_id, decision_id, generation, expected_revision, claimant_kind, claimant_id, bundle_json, bundle_sha256, effect_receipt_json, effect_receipt_sha256, state, committed_at, acknowledged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, NULL)",
                (
                    bundle.action_id,
                    str(bundle.decision_id),
                    int(bundle.generation),
                    decision_revision,
                    str(decision["claimant_kind"]),
                    claimant_id,
                    bundle_json,
                    bundle.sha256,
                    receipt_json,
                    receipt_digest,
                    current,
                ),
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = 'action_committed', revision = ?, action_id = ?, action_bundle_json = ?, action_bundle_sha256 = ?, committed_at = ?, claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND revision = ?",
                (
                    decision_revision + 1,
                    bundle.action_id,
                    bundle_json,
                    bundle.sha256,
                    current,
                    current,
                    str(bundle.decision_id),
                    decision_revision,
                ),
            )
            return ControllerActionReceipt(
                bundle.action_id,
                bundle.decision_id,
                bundle.generation,
                decision_revision,
                bundle.sha256,
                receipt_payload,
                "committed",
                current,
            )

    def complete_program_integration(
        self,
        program_id: ProgramId | str,
        milestone_id: MilestoneId | str,
        *,
        candidate_sha: str,
        receipt: Mapping[str, object],
        state: str = "applied",
        now: str | None = None,
    ) -> ProgramStatus:
        """Commit one verified Git integration receipt and advance readiness."""

        program = ProgramId(str(program_id))
        milestone = MilestoneId(str(milestone_id))
        if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
            raise ValueError("program integration candidate is invalid")
        if state not in {"applied", "conflict", "failed"}:
            raise ValueError("program integration state is unsupported")
        if not isinstance(receipt, Mapping):
            raise TypeError("program integration receipt must be an object")
        try:
            receipt_value = dict(receipt)
            receipt_json = _encode_json(receipt_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("program integration receipt is not JSON") from exc
        current = now or utc_now()
        with self._transaction():
            program_row = (
                self._db()
                .execute("SELECT * FROM runs WHERE run_id = ? AND program_digest IS NOT NULL", (str(program),))
                .fetchone()
            )
            if program_row is None:
                raise RecordNotFound(f"program does not exist: {program}")
            integration_id = f"integration/{program}/{milestone}/{candidate_sha}"
            pending = (
                self._db()
                .execute("SELECT * FROM integration_outbox WHERE integration_id = ?", (integration_id,))
                .fetchone()
            )
            if pending is None:
                raise RecordNotFound(f"program integration does not exist: {integration_id}")
            expected = {
                "program_id": str(program),
                "milestone_id": str(milestone),
                "candidate_sha": candidate_sha,
                "expected_trunk_head": str(pending["expected_trunk_head"]),
                "strategy": str(pending["strategy"]),
            }
            if any(receipt_value.get(key) != value for key, value in expected.items()):
                raise StaleWriter("program integration receipt does not match its authorized outbox")
            before = receipt_value.get("before_trunk_head")
            after = receipt_value.get("after_trunk_head")
            if before != expected["expected_trunk_head"]:
                raise StaleWriter("program integration receipt has a stale trunk predecessor")
            if state == "applied" and (not isinstance(after, str) or re.fullmatch(r"[0-9a-f]{40}", after) is None):
                raise ValueError("applied program integration requires a resolved after trunk HEAD")
            if state == "applied":
                parents = receipt_value.get("parents")
                tree = receipt_value.get("tree")
                if (
                    not isinstance(parents, list)
                    or not parents
                    or any(
                        not isinstance(parent, str) or re.fullmatch(r"[0-9a-f]{40}", parent) is None
                        for parent in parents
                    )
                ):
                    raise ValueError("applied program integration requires exact commit parents")
                if not isinstance(tree, str) or re.fullmatch(r"[0-9a-f]{40}", tree) is None:
                    raise ValueError("applied program integration requires an exact commit tree")
                strategy = expected["strategy"]
                logical_promotion = receipt_value.get("logical_promotion") is True
                if logical_promotion and after != candidate_sha:
                    raise StaleWriter("logical promotion receipt has an unexpected resulting HEAD")
                if logical_promotion:
                    lineage = receipt_value.get("lineage")
                    if (
                        receipt_value.get("linear_chain") is not True
                        or not isinstance(lineage, list)
                        or len(lineage) < 2
                        or any(
                            not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{40}", item) is None for item in lineage
                        )
                        or lineage[0] != expected["expected_trunk_head"]
                        or lineage[-1] != candidate_sha
                        or len(set(lineage)) != len(lineage)
                        or parents != [lineage[-2]]
                    ):
                        raise StaleWriter("logical promotion receipt has an invalid linear candidate chain")
                if not logical_promotion and strategy == "fast_forward" and after != candidate_sha:
                    raise StaleWriter("fast-forward integration receipt has an unexpected resulting HEAD")
                if (
                    not logical_promotion
                    and strategy == "merge"
                    and parents != [expected["expected_trunk_head"], candidate_sha]
                ):
                    raise StaleWriter("merge integration receipt has unexpected commit parents")
                if not logical_promotion and strategy == "cherry_pick" and parents != [expected["expected_trunk_head"]]:
                    raise StaleWriter("cherry-pick integration receipt has unexpected commit parents")
            if (
                state == "applied"
                and str(pending["state"]) == "pending"
                and str(program_row["trunk_head"]) != expected["expected_trunk_head"]
            ):
                raise StaleWriter("program integration trunk authority changed before receipt commit")
            if str(pending["state"]) != "pending":
                if str(pending["receipt_sha256"]) != hashlib.sha256(receipt_json.encode("utf-8")).hexdigest():
                    raise StaleWriter("program integration already has a different terminal receipt")
                return self._program_status_from_row(program_row)
            digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            self._db().execute(
                "UPDATE integration_outbox SET state = ?, receipt_json = ?, receipt_sha256 = ?, completed_at = ? "
                "WHERE integration_id = ? AND state = 'pending'",
                (state, receipt_json, digest, current, integration_id),
            )
            if state == "applied":
                all_integrations = (
                    self._db()
                    .execute(
                        "SELECT COUNT(*) FROM integration_outbox WHERE program_id = ? AND state = 'applied'",
                        (str(program),),
                    )
                    .fetchone()
                )
                total_nodes = len(
                    self._program_node_payloads(strict_json_loads(str(program_row["program_graph_json"])))
                )
                next_state = (
                    ProgramState.COMPLETED.value
                    if int(all_integrations[0]) == total_nodes
                    else ProgramState.RUNNING.value
                )
                self._db().execute(
                    "UPDATE runs SET trunk_head = ?, program_revision = program_revision + 1, program_state = ?, "
                    "closed_at = CASE WHEN ? = 'completed' THEN COALESCE(closed_at, ?) ELSE closed_at END WHERE run_id = ?",
                    (after, next_state, next_state, current, str(program)),
                )
                self._record_program_fact_in_transaction(
                    program,
                    milestone,
                    phase=LifecyclePhase.ACCEPTANCE,
                    kind="integration_completed",
                    data={"candidate_sha": candidate_sha, "receipt_sha256": digest, "after_trunk_head": after},
                )
                self._ensure_program_decision_in_transaction(
                    program,
                    event_kind=ProgramEventKind.INTEGRATION_COMPLETED,
                    event_key=f"milestone/{milestone}/integration/{candidate_sha}",
                    payload={
                        "milestone_id": str(milestone),
                        "candidate_sha": candidate_sha,
                        "receipt_sha256": digest,
                        "after_trunk_head": after,
                    },
                )
            else:
                self._db().execute(
                    "UPDATE runs SET program_state = ? WHERE run_id = ?",
                    (
                        ProgramState.NEEDS_DECISION.value if state == "conflict" else ProgramState.FAILED.value,
                        str(program),
                    ),
                )
                # Integration is an external effect of a committed program
                # action.  Its action-bound failed-effect decision is the sole
                # recovery authority; emitting another milestone decision here
                # would create two independent claims for one failure.
            refreshed = self._db().execute("SELECT * FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            assert refreshed is not None
            return self._program_status_from_row(refreshed)

    def pending_program_integrations(self, program_id: ProgramId | str | None = None) -> tuple[JsonObject, ...]:
        """Read authorized, not-yet-applied Git integration effects."""

        query = "SELECT * FROM integration_outbox WHERE state = 'pending'"
        args: tuple[object, ...] = ()
        if program_id is not None:
            query += " AND program_id = ?"
            args = (str(ProgramId(str(program_id))),)
        query += " ORDER BY created_at, integration_id"
        return tuple(self._queue_row(row) for row in self._db().execute(query, args).fetchall())

    def program_action_outboxes(self, program_id: ProgramId | str | None = None) -> tuple[JsonObject, ...]:
        """Read durable program action receipts for idempotent effect replay."""

        query = (
            "SELECT a.*, d.program_id, d.event_kind, d.event_key FROM controller_action_outbox a "
            "JOIN controller_decisions d ON d.decision_id = a.decision_id WHERE d.program_id IS NOT NULL"
        )
        args: tuple[object, ...] = ()
        if program_id is not None:
            query += " AND d.program_id = ?"
            args = (str(ProgramId(str(program_id))),)
        query += " ORDER BY a.committed_at, a.action_id"
        return tuple(self._queue_row(row) for row in self._db().execute(query, args).fetchall())

    def _program_action_effect_binding(
        self, action_id: str, effect_id: str
    ) -> tuple[ProgramId, MilestoneId, ModelFacingProgramControllerActionBundle]:
        row = (
            self._db()
            .execute(
                "SELECT a.bundle_json, d.program_id FROM controller_action_outbox a "
                "JOIN controller_decisions d ON d.decision_id = a.decision_id "
                "WHERE a.action_id = ? AND d.program_id IS NOT NULL",
                (action_id,),
            )
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"program action does not exist: {action_id}")
        bundle = ModelFacingProgramControllerActionBundle.from_json_bytes(str(row["bundle_json"]))
        bindings = dict(bundle.external_effects)
        milestone_id = bindings.get(effect_id)
        if milestone_id is None:
            raise StaleWriter("program action effect is not authorized by its bundle")
        return ProgramId(str(row["program_id"])), MilestoneId(milestone_id), bundle

    def program_action_effect_state(self, action_id: str, effect_id: str) -> str:
        """Read one durable external-effect state from existing lifecycle facts."""

        program, milestone, _bundle = self._program_action_effect_binding(action_id, effect_id)
        for fact in reversed(self.review_lifecycle(program, milestone)):
            if fact.data.get("action_id") != action_id or fact.data.get("effect_id") != effect_id:
                continue
            if fact.kind == "program_effect_applied":
                return "applied"
            if fact.kind == "program_effect_failed":
                return "failed"
        return "pending"

    def record_program_action_effect(
        self,
        action_id: str,
        effect_id: str,
        *,
        state: str,
        error_code: str | None = None,
    ) -> str:
        """Commit one idempotent effect outcome and surface typed failure."""

        if state not in {"applied", "failed"}:
            raise ValueError("program action effect state is unsupported")
        if state == "failed":
            if error_code is None or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_code) is None:
                raise ValueError("program action effect failure code is invalid")
        elif error_code is not None:
            raise ValueError("applied program action effect cannot carry an error")
        with self._transaction():
            program, milestone, bundle = self._program_action_effect_binding(action_id, effect_id)
            existing = self.program_action_effect_state(action_id, effect_id)
            if existing == state:
                self._acknowledge_program_action_if_terminal_in_transaction(action_id, bundle)
                return existing
            if existing != "pending":
                raise StaleWriter("program action effect already has a different terminal outcome")
            current = utc_now()
            data: JsonObject = {
                "action_id": action_id,
                "effect_id": effect_id,
                "error_code": error_code,
            }
            self._record_program_fact_in_transaction(
                program,
                milestone,
                phase=LifecyclePhase.ROUTING,
                kind=f"program_effect_{state}",
                data=data,
            )
            if state == "failed":
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'human_attention_required', "
                    "human_attention_reason = ?, claim_lease_expires_at = NULL, updated_at = ? "
                    "WHERE action_id = ? AND state = 'action_committed'",
                    (error_code, current, action_id),
                )
                self._db().execute(
                    "UPDATE runs SET program_state = 'needs_decision' WHERE run_id = ?",
                    (str(program),),
                )
                self._ensure_program_decision_in_transaction(
                    program,
                    event_kind=ProgramEventKind.CONTROLLER_ATTENTION,
                    event_key=f"action-effect/{action_id}/failed",
                    payload={
                        "action_id": action_id,
                        "effect_id": effect_id,
                        "error_code": error_code,
                    },
                )
            self._acknowledge_program_action_if_terminal_in_transaction(action_id, bundle, now=current)
            return state

    def _acknowledge_program_action_if_terminal_in_transaction(
        self,
        action_id: str,
        bundle: ModelFacingProgramControllerActionBundle,
        *,
        now: str | None = None,
    ) -> bool:
        """Close a program action once every authorized external effect is terminal."""

        if not bundle.external_effects:
            return False
        if any(
            self.program_action_effect_state(action_id, effect_id) == "pending"
            for effect_id, _milestone_id in bundle.external_effects
        ):
            return False
        current = now or utc_now()
        updated = self._db().execute(
            "UPDATE controller_action_outbox SET state = 'acknowledged', acknowledged_at = ? "
            "WHERE action_id = ? AND state = 'committed'",
            (current, action_id),
        )
        if updated.rowcount == 1:
            self._db().execute(
                "UPDATE controller_decisions SET state = CASE WHEN state = 'human_attention_required' "
                "THEN 'human_attention_required' ELSE 'acknowledged' END, acknowledged_at = ?, "
                "claim_lease_expires_at = NULL, updated_at = ? "
                "WHERE action_id = ? AND state IN ('action_committed', 'human_attention_required')",
                (current, current, action_id),
            )
        return updated.rowcount == 1

    def record_program_attention(
        self,
        program_id: ProgramId | str,
        *,
        event_key: str,
        payload: Mapping[str, object],
    ) -> ProgramControllerDecisionStatus:
        """Emit one coalesced controller-attention event."""

        program = ProgramId(str(program_id))
        with self._transaction():
            row = self._db().execute("SELECT program_state FROM runs WHERE run_id = ?", (str(program),)).fetchone()
            if row is None:
                raise RecordNotFound(f"program does not exist: {program}")
            self._db().execute(
                "UPDATE runs SET program_state = 'needs_decision' WHERE run_id = ? AND program_state NOT IN ('completed', 'failed', 'external_blocked')",
                (str(program),),
            )
            return self._ensure_program_decision_in_transaction(
                program,
                event_kind=ProgramEventKind.CONTROLLER_ATTENTION,
                event_key=event_key,
                payload=payload,
            )

    def controller_decision(self, decision_id: ControllerDecisionId | str) -> ControllerDecisionStatus:
        identity = ControllerDecisionId(str(decision_id))
        row = (
            self._db()
            .execute(
                "SELECT * FROM controller_decisions WHERE decision_id = ? AND program_id IS NULL", (str(identity),)
            )
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"controller decision does not exist: {identity}")
        return self._decision_status_from_row(row)

    def controller_decisions(
        self, *, state: ControllerDecisionState | str | None = None
    ) -> tuple[ControllerDecisionStatus, ...]:
        query = "SELECT * FROM controller_decisions WHERE program_id IS NULL"
        args: tuple[object, ...] = ()
        if state is not None:
            state_value = (
                state.value if isinstance(state, ControllerDecisionState) else ControllerDecisionState(state).value
            )
            query += " AND state = ?"
            args = (state_value,)
        query += " ORDER BY deadline, decision_id"
        return tuple(self._decision_status_from_row(row) for row in self._db().execute(query, args).fetchall())

    def controller_action_pending_acknowledgement(self, decision_id: ControllerDecisionId | str) -> bool:
        """Return whether the decision's immutable effect still owes its ack."""

        identity = ControllerDecisionId(str(decision_id))
        row = (
            self._db()
            .execute(
                "SELECT a.state FROM controller_decisions d "
                "JOIN controller_action_outbox a ON a.action_id = d.action_id AND a.decision_id = d.decision_id "
                "WHERE d.decision_id = ?",
                (str(identity),),
            )
            .fetchone()
        )
        return row is not None and str(row["state"]) == "committed"

    def controller_generation(
        self, decision_id: ControllerDecisionId | str, generation: Generation | int | None = None
    ) -> ControllerGenerationStatus:
        """Return one durable controller-generation lineage record."""

        identity = ControllerDecisionId(str(decision_id))
        if generation is None:
            decision = (
                self._db()
                .execute("SELECT current_generation FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            generation_value = int(decision["current_generation"])
        else:
            generation_value = int(Generation(generation))
        row = (
            self._db()
            .execute(
                "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                (str(identity), generation_value),
            )
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
        return self._controller_generation_status_from_row(row)

    def prepare_controller_generation(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        prompt_sha256: str | None = None,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Reserve one generation launch before opening an SDK writer.

        The transition is idempotent for the same prepared launch.  A
        ``delivery_starting`` row is deliberately left durable across a
        process crash; recovery must inspect its persisted identity before
        creating another writer.
        """

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        if prompt_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None:
            raise ValueError("controller prompt digest is invalid")
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller generation is stale")
            if str(decision["state"]) not in {
                ControllerDecisionState.CLAIMED.value,
                ControllerDecisionState.AWAITING_CLAIM.value,
            }:
                raise StaleWriter("controller decision is not launchable")
            authority = (
                self._db().execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is not None and int(authority["requested_shutdown"]) == 1:
                raise HarnessRefreshBlocked("harness refresh fence is active")
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if row is None:
                raise CorruptSchemaError("controller decision generation is missing")
            state = str(row["state"])
            if state not in {
                ControllerGenerationState.PREPARED.value,
                ControllerGenerationState.DELIVERY_STARTING.value,
            }:
                raise StaleWriter("controller generation is already terminal or active")
            existing_prompt = row["prompt_sha256"]
            if existing_prompt is not None and prompt_sha256 is not None and str(existing_prompt) != prompt_sha256:
                raise StaleWriter("controller generation prompt digest conflicts")
            target_prompt = prompt_sha256 if prompt_sha256 is not None else existing_prompt
            self._db().execute(
                "UPDATE controller_decision_generations SET state = 'delivery_starting', prompt_sha256 = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND state IN ('prepared','delivery_starting')",
                (target_prompt, current, str(identity), int(generation_value)),
            )
            updated = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert updated is not None
            return self._controller_generation_status_from_row(updated)

    @staticmethod
    def _controller_generation_launch_fingerprint(
        decision: sqlite3.Row,
        generation: sqlite3.Row,
    ) -> str:
        """Digest the exact scheduler/model authority observed before launch."""

        payload = {
            "decision": {
                "revision": int(decision["revision"]),
                "state": str(decision["state"]),
                "current_generation": int(decision["current_generation"]),
                "claimant_kind": decision["claimant_kind"],
                "claimant_id": decision["claimant_id"],
                "claim_token_sha256": decision["claim_token_sha256"],
                "claim_lease_expires_at": decision["claim_lease_expires_at"],
                "action_id": decision["action_id"],
                "action_bundle_sha256": decision["action_bundle_sha256"],
            },
            "generation": {
                "generation": int(generation["generation"]),
                "state": str(generation["state"]),
                "prompt_sha256": generation["prompt_sha256"],
                "controller_thread_id": generation["controller_thread_id"],
                "controller_turn_id": generation["controller_turn_id"],
                "inspection_outcome": generation["inspection_outcome"],
                "inspection_bundle_sha256": generation["inspection_bundle_sha256"],
            },
        }
        return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _require_controller_generation_launch_owner(decision: sqlite3.Row, *, now: str) -> None:
        """Reject ownership that cannot authorize a scheduler/model child."""

        state = ControllerDecisionState(str(decision["state"]))
        if state is ControllerDecisionState.AWAITING_CLAIM:
            if any(
                decision[field] is not None
                for field in ("claimant_kind", "claimant_id", "claim_token_sha256", "claim_lease_expires_at")
            ):
                raise StaleWriter("controller launch has conflicting unclaimed authority")
            return
        if state is ControllerDecisionState.CLAIMED:
            if (
                decision["claimant_kind"] != ControllerClaimantKind.MODEL.value
                or decision["claimant_id"] is None
                or decision["claim_token_sha256"] is None
                or decision["claim_lease_expires_at"] is None
                or str(decision["claim_lease_expires_at"]) <= now
            ):
                raise StaleWriter("controller launch is not owned by a live model claim")
            return
        if state is ControllerDecisionState.ACTION_COMMITTED:
            if decision["action_id"] is None or decision["action_bundle_sha256"] is None:
                raise StaleWriter("controller acknowledgement recovery lacks an action")
            return
        if state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED:
            if decision["action_id"] is None or decision["action_bundle_sha256"] is None:
                raise StaleWriter("uncommitted human attention cannot launch a controller child")
            return
        raise StaleWriter("controller decision is not scheduler/model launchable")

    def _controller_generation_launch_authority(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        now: str | None = None,
    ) -> str:
        """Capture one exact opaque pre-``Popen`` ownership snapshot."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if decision is None or row is None:
                raise RecordNotFound(f"controller launch authority does not exist: {identity}/{generation_value}")
            authority = (
                self._db().execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is not None and int(authority["requested_shutdown"]) == 1:
                raise HarnessRefreshBlocked("harness refresh fence is active")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller launch generation is stale")
            self._require_controller_generation_launch_owner(decision, now=current)
            return self._controller_generation_launch_fingerprint(decision, row)

    def _bind_controller_generation_launch(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        expected_authority_sha256: str,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """CAS-bind a successful process launch to unchanged durable ownership."""

        if re.fullmatch(r"[0-9a-f]{64}", expected_authority_sha256) is None:
            raise ValueError("controller launch authority digest is invalid")
        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if decision is None or row is None:
                raise RecordNotFound(f"controller launch authority does not exist: {identity}/{generation_value}")
            authority = (
                self._db().execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()
            )
            if authority is not None and int(authority["requested_shutdown"]) == 1:
                raise HarnessRefreshBlocked("harness refresh fence is active")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller launch generation is stale")
            self._require_controller_generation_launch_owner(decision, now=current)
            if self._controller_generation_launch_fingerprint(decision, row) != expected_authority_sha256:
                raise StaleWriter("controller launch ownership changed during process creation")
            updated = self._db().execute(
                "UPDATE controller_decision_generations SET updated_at = ? WHERE decision_id = ? AND generation = ? AND updated_at = ?",
                (current, str(identity), int(generation_value), str(row["updated_at"])),
            )
            if updated.rowcount != 1:
                raise StaleWriter("controller launch generation CAS is stale")
            bound = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert bound is not None
            return self._controller_generation_status_from_row(bound)

    def bind_controller_generation_thread(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        controller_thread_id: ThreadIdentity | str,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Persist the SDK thread identity before accepting model output."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        thread = (
            controller_thread_id
            if isinstance(controller_thread_id, ThreadIdentity)
            else ThreadIdentity(controller_thread_id)
        )
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT current_generation FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller generation is stale")
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
            if row["controller_thread_id"] is not None:
                if str(row["controller_thread_id"]) != thread.id:
                    raise StaleWriter("controller thread identity is immutable")
                return self._controller_generation_status_from_row(row)
            if str(row["state"]) != ControllerGenerationState.DELIVERY_STARTING.value:
                raise StaleWriter("controller generation is not awaiting thread identity")
            updated = self._db().execute(
                "UPDATE controller_decision_generations SET state = 'active', controller_thread_id = ?, started_at = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND controller_thread_id IS NULL AND state = 'delivery_starting'",
                (thread.id, current, current, str(identity), int(generation_value)),
            )
            if updated.rowcount != 1:
                raise StaleWriter("controller thread identity bind is stale")
            bound = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert bound is not None
            return self._controller_generation_status_from_row(bound)

    def bind_controller_generation_turn(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        controller_thread_id: ThreadIdentity | str,
        controller_turn_id: str,
        previous_controller_turn_id: str | None = None,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Persist the current SDK turn, CAS-replacing a bounded retry turn."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        thread = (
            controller_thread_id
            if isinstance(controller_thread_id, ThreadIdentity)
            else ThreadIdentity(controller_thread_id)
        )
        if (
            not isinstance(controller_turn_id, str)
            or not controller_turn_id
            or len(controller_turn_id.encode("utf-8")) > 512
            or any(c.isspace() or ord(c) < 0x20 for c in controller_turn_id)
        ):
            raise ValueError("controller turn identity is invalid")
        if previous_controller_turn_id is not None and (
            not previous_controller_turn_id
            or len(previous_controller_turn_id.encode("utf-8")) > 512
            or any(c.isspace() or ord(c) < 0x20 for c in previous_controller_turn_id)
        ):
            raise ValueError("previous controller turn identity is invalid")
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT current_generation FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller generation is stale")
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
            if row["controller_thread_id"] != thread.id:
                raise StaleWriter("controller generation thread identity is stale")
            if row["controller_turn_id"] is not None:
                if str(row["controller_turn_id"]) == controller_turn_id:
                    return self._controller_generation_status_from_row(row)
                if previous_controller_turn_id != str(row["controller_turn_id"]):
                    raise StaleWriter("controller retry turn identity is stale")
            elif previous_controller_turn_id is not None:
                raise StaleWriter("controller retry turn predecessor is stale")
            updated_count = self._db().execute(
                "UPDATE controller_decision_generations SET controller_turn_id = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND state = 'active' "
                "AND ((controller_turn_id IS NULL AND ? IS NULL) OR controller_turn_id = ?)",
                (
                    controller_turn_id,
                    current,
                    str(identity),
                    int(generation_value),
                    previous_controller_turn_id,
                    previous_controller_turn_id,
                ),
            )
            if updated_count.rowcount != 1:
                raise StaleWriter("controller retry turn bind is stale")
            updated = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert updated is not None
            return self._controller_generation_status_from_row(updated)

    def complete_controller_generation(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        state: ControllerGenerationState | str,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Persist one terminal generation outcome without inferring effects."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        outcome = state if isinstance(state, ControllerGenerationState) else ControllerGenerationState(state)
        if outcome not in {
            ControllerGenerationState.COMPLETED,
            ControllerGenerationState.FAILED,
            ControllerGenerationState.INTERRUPTED,
            ControllerGenerationState.UNAVAILABLE,
            ControllerGenerationState.AMBIGUOUS,
            ControllerGenerationState.SUPERSEDED,
        }:
            raise ValueError("controller generation outcome is not terminal")
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT current_generation FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller generation is stale")
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
            if str(row["state"]) in {
                item.value
                for item in ControllerGenerationState
                if item
                not in {
                    ControllerGenerationState.PREPARED,
                    ControllerGenerationState.DELIVERY_STARTING,
                    ControllerGenerationState.ACTIVE,
                }
            }:
                if str(row["state"]) != outcome.value:
                    raise StaleWriter("controller generation terminal state is immutable")
                return self._controller_generation_status_from_row(row)
            self._db().execute(
                "UPDATE controller_decision_generations SET state = ?, terminal_at = ?, updated_at = ? WHERE decision_id = ? AND generation = ?",
                (outcome.value, current, current, str(identity), int(generation_value)),
            )
            updated = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert updated is not None
            return self._controller_generation_status_from_row(updated)

    def record_controller_profile_drift(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        reason: str = "native compatibility profile changed before SDK identity",
        now: str | None = None,
    ) -> ControllerDecisionStatus:
        """Close one known pre-identity profile drift as human attention.

        A controller child can fail while constructing its SDK adapter, before
        it has a thread identity.  That is a known configuration fact, not an
        uncertain launch that may be retried.  Keep the existing generation
        and decision rows authoritative and close them atomically.
        """

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512 or "\x00" in reason:
            raise ValueError("controller profile-drift reason is invalid")
        reason = redact_control_text(reason, limit=512)
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            generation_row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if decision is None or generation_row is None:
                raise RecordNotFound(
                    f"controller profile-drift authority does not exist: {identity}/{generation_value}"
                )
            if int(decision["current_generation"]) != int(generation_value):
                raise StaleWriter("controller profile-drift generation is stale")
            if (
                str(generation_row["state"]) == ControllerGenerationState.FAILED.value
                and generation_row["controller_thread_id"] is None
                and generation_row["inspection_outcome"] is None
                and str(decision["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
            ):
                return self._decision_status_from_row(decision)
            if (
                str(generation_row["state"]) != ControllerGenerationState.DELIVERY_STARTING.value
                or generation_row["controller_thread_id"] is not None
                or generation_row["inspection_outcome"] is not None
            ):
                raise StaleWriter("controller profile drift is not a pre-identity generation")
            if str(decision["state"]) not in {
                ControllerDecisionState.AWAITING_CLAIM.value,
                ControllerDecisionState.CLAIMED.value,
            }:
                raise StaleWriter("controller profile drift decision is no longer launchable")
            self._db().execute(
                "UPDATE controller_decision_generations SET state = 'failed', terminal_at = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND state = 'delivery_starting' AND controller_thread_id IS NULL",
                (current, current, str(identity), int(generation_value)),
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = 'human_attention_required', revision = revision + 1, "
                "claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, "
                "claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? "
                "WHERE decision_id = ? AND current_generation = ? AND state IN ('awaiting_claim','claimed')",
                (reason, current, str(identity), int(generation_value)),
            )
            updated = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            assert updated is not None
            return self._decision_status_from_row(updated)

    def authorize_controller_profile_refresh(
        self,
        *,
        native_profile_sha256: str,
        native_compatibility_sha256: str,
        now: str | None = None,
    ) -> int:
        """Rotate only pre-identity controller decisions during explicit refresh.

        The service refresh command is the operator authorization boundary.
        It may replace stale profile identities only where no worker or
        controller SDK identity exists. Permission facts, plugin snapshots,
        capsule bytes and every other route field remain immutable.
        """

        profile = _sha256(native_profile_sha256, field_name="refreshed native profile digest")
        compatibility = _sha256(
            native_compatibility_sha256,
            field_name="refreshed native compatibility digest",
        )
        current = now or utc_now()
        deadline = self._expires_after(current, _CONTROLLER_DECISION_WINDOW_SECONDS)
        changed = 0
        with self._transaction():
            rows = (
                self._db()
                .execute(
                    "SELECT d.*, g.state AS generation_state, g.controller_thread_id, g.controller_turn_id, "
                    "q.route_json, b.native_profile_sha256 AS bound_profile_sha256, "
                    "b.native_compatibility_sha256 AS bound_compatibility_sha256 "
                    "FROM controller_decisions d "
                    "JOIN controller_decision_generations g ON g.decision_id = d.decision_id "
                    "AND g.generation = d.current_generation "
                    "JOIN dispatch_queue q ON q.dispatch_id = d.dispatch_id "
                    "JOIN queue_bindings b ON b.dispatch_id = d.dispatch_id "
                    "LEFT JOIN worker_liveness l ON l.dispatch_id = d.dispatch_id AND l.exited_at IS NULL "
                    "WHERE l.dispatch_id IS NULL AND d.action_id IS NULL "
                    "AND d.claimant_kind IS NULL AND d.claim_token_sha256 IS NULL "
                    "AND g.controller_thread_id IS NULL AND g.controller_turn_id IS NULL "
                    "AND ((d.state = 'awaiting_claim' AND g.state = 'prepared') "
                    "OR (d.state = 'human_attention_required' AND g.state = 'failed' "
                    "AND d.human_attention_reason = 'native compatibility profile changed before SDK identity')) "
                    "ORDER BY d.decision_id"
                )
                .fetchall()
            )
            for row in rows:
                route = _decode_json_object(str(row["route_json"]), field_name="controller queue route")
                if route is None:
                    raise CorruptSchemaError("controller queue route is missing")
                if (
                    route.get("native_profile_sha256") != row["bound_profile_sha256"]
                    or route.get("native_compatibility_sha256") != row["bound_compatibility_sha256"]
                ):
                    raise CorruptSchemaError("controller queue profile authority is inconsistent")
                if (
                    row["bound_profile_sha256"] == profile
                    and row["bound_compatibility_sha256"] == compatibility
                    and str(row["state"]) == ControllerDecisionState.AWAITING_CLAIM.value
                    and str(row["generation_state"]) == ControllerGenerationState.PREPARED.value
                ):
                    continue
                refreshed_route = dict(route)
                refreshed_route["native_profile_sha256"] = profile
                refreshed_route["native_compatibility_sha256"] = compatibility
                self._db().execute(
                    "UPDATE dispatch_queue SET route_json = ?, updated_at = ? WHERE dispatch_id = ?",
                    (_encode_json(refreshed_route), current, str(row["dispatch_id"])),
                )
                self._db().execute(
                    "UPDATE queue_bindings SET native_profile_sha256 = ?, native_compatibility_sha256 = ?, "
                    "updated_at = ? WHERE dispatch_id = ?",
                    (profile, compatibility, current, str(row["dispatch_id"])),
                )
                self._db().execute(
                    "UPDATE recovery_state SET profile_sha256 = ?, updated_at = ? "
                    "WHERE dispatch_id = ? AND profile_sha256 IS NOT NULL",
                    (profile, current, str(row["dispatch_id"])),
                )
                if str(row["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value:
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, "
                        "deadline = CASE WHEN deadline < ? THEN ? ELSE deadline END, "
                        "human_attention_reason = NULL, updated_at = ? WHERE decision_id = ?",
                        (deadline, deadline, current, str(row["decision_id"])),
                    )
                    self._db().execute(
                        "UPDATE controller_decision_generations SET state = 'prepared', terminal_at = NULL, "
                        "updated_at = ? WHERE decision_id = ? AND generation = ?",
                        (current, str(row["decision_id"]), int(row["current_generation"])),
                    )
                changed += 1
            return changed

    def authorize_ambiguous_controller_replacement(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        expected_revision: int,
        reason: str,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Authorize one replacement after a consumed ambiguous inspection.

        This is an explicit operator boundary. It cannot replace an active
        controller, an uninspected identity, a committed action or a decision
        without remaining generation budget.
        """

        identity = ControllerDecisionId(str(decision_id))
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512 or "\x00" in reason:
            raise ValueError("controller replacement reason is invalid")
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["revision"]) != expected_revision:
                raise StaleWriter("controller replacement revision is stale")
            generation = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(decision["current_generation"])),
                )
                .fetchone()
            )
            if generation is None:
                raise CorruptSchemaError("controller replacement generation is missing")
            if (
                str(decision["state"]) != ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                or decision["action_id"] is not None
                or decision["claimant_kind"] is not None
                or str(generation["state"]) != ControllerGenerationState.AMBIGUOUS.value
                or str(generation["inspection_outcome"]) != ControllerGenerationState.AMBIGUOUS.value
                or generation["inspection_completed_at"] is None
            ):
                raise StaleWriter("controller replacement lacks exact ambiguous inspection authority")
            next_generation = int(decision["current_generation"]) + 1
            if next_generation > int(decision["generation_budget"]):
                raise StaleWriter("controller replacement generation budget is exhausted")
            self._db().execute(
                "INSERT INTO controller_decision_generations(decision_id, generation, lineage_id, "
                "predecessor_generation, source_kind, state, prompt_sha256, controller_thread_id, "
                "controller_turn_id, inspection_started_at, inspection_token_sha256, "
                "inspection_lease_expires_at, inspection_completed_at, inspection_outcome, "
                "inspection_bundle_json, inspection_bundle_sha256, started_at, terminal_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'replacement_controller', 'prepared', NULL, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    str(identity),
                    next_generation,
                    str(generation["lineage_id"]),
                    int(generation["generation"]),
                    current,
                    current,
                ),
            )
            deadline = self._expires_after(current, _CONTROLLER_DECISION_WINDOW_SECONDS)
            self._db().execute(
                "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, "
                "current_generation = ?, deadline = ?, human_attention_reason = NULL, updated_at = ? "
                "WHERE decision_id = ? AND revision = ?",
                (next_generation, deadline, current, str(identity), expected_revision),
            )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), next_generation),
                )
                .fetchone()
            )
            assert row is not None
            return self._controller_generation_status_from_row(row)

    def authorize_controller_replan_after_local_pre_turn_failure(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        expected_revision: int,
        reason: str,
        now: str | None = None,
    ) -> ControllerDecisionStatus:
        """Start one new decision cycle after every prior generation had no turn.

        This operator-only escape hatch is narrower than raising a generation
        budget: every consumed generation must have an ambiguous inspection
        and no persisted turn identity. The old decision is retained as
        superseded provenance and the new cycle receives its own bounded
        generation budget.
        """

        identity = ControllerDecisionId(str(decision_id))
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 512 or "\x00" in reason:
            raise ValueError("controller replan reason is invalid")
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["revision"]) != expected_revision:
                raise StaleWriter("controller replan revision is stale")
            generations = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? ORDER BY generation",
                    (str(identity),),
                )
                .fetchall()
            )
            if (
                str(decision["state"]) != ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                or decision["action_id"] is not None
                or decision["claimant_kind"] is not None
                or int(decision["current_generation"]) != int(decision["generation_budget"])
                or len(generations) != int(decision["generation_budget"])
                or any(
                    str(row["state"]) != ControllerGenerationState.AMBIGUOUS.value
                    or str(row["inspection_outcome"]) != ControllerGenerationState.AMBIGUOUS.value
                    or row["inspection_completed_at"] is None
                    or row["controller_turn_id"] is not None
                    for row in generations
                )
            ):
                raise StaleWriter("controller replan lacks exact no-turn exhausted authority")
            latest_cycle = int(
                self._db()
                .execute(
                    "SELECT MAX(cycle_sequence) FROM controller_decisions WHERE dispatch_id = ? AND kind = ?",
                    (str(decision["dispatch_id"]), str(decision["kind"])),
                )
                .fetchone()[0]
            )
            next_cycle = latest_cycle + 1
            if next_cycle > 64:
                raise StaleWriter("controller decision cycle budget is exhausted")
            self._db().execute(
                "UPDATE controller_decisions SET state = 'superseded', superseded_at = ?, "
                "human_attention_reason = NULL, updated_at = ? WHERE decision_id = ? AND revision = ?",
                (current, current, str(identity), expected_revision),
            )
            dispatch_id = str(decision["dispatch_id"])
            payload = {
                "schema_version": 1,
                "kind": "CHECKPOINT",
                "delivery_id": f"wake/{dispatch_id}/checkpoint/{next_cycle}",
                "dispatch_id": dispatch_id,
                "cycle_sequence": next_cycle,
                "state": str(self.queue_dispatch(dispatch_id)["state"]),
                "ledger_path": os.fspath(self.path),
            }
            deadline = self._expires_after(current, _CONTROLLER_DECISION_WINDOW_SECONDS)
            next_decision_id = self._ensure_controller_decision_in_transaction(
                dispatch_id=dispatch_id,
                kind="checkpoint",
                cycle_sequence=next_cycle,
                payload=payload,
                source_thread_id=str(decision["source_thread_id"]) if decision["source_thread_id"] else None,
                deadline=deadline,
                wake_state="suppressed",
                queue_state=str(payload["state"]),
            )
            encoded = _encode_json(payload)
            self._db().execute(
                "INSERT INTO wake_outbox(delivery_id, dispatch_id, decision_id, kind, cycle_sequence, "
                "source_thread_id, payload_json, payload_digest, state, attempt_count, source_turn_id, "
                "created_at, updated_at) VALUES (?, ?, ?, 'checkpoint', ?, ?, ?, ?, 'suppressed', 0, NULL, ?, ?)",
                (
                    str(payload["delivery_id"]),
                    dispatch_id,
                    next_decision_id,
                    next_cycle,
                    decision["source_thread_id"],
                    encoded,
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    current,
                    current,
                ),
            )
            self._db().execute(
                "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0, updated_at = ? "
                "WHERE dispatch_id = ?",
                (current, dispatch_id),
            )
            return self.controller_decision(next_decision_id)

    def reset_controller_generation_delivery(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        expected_revision: int,
        claimant_id: str,
        token: str,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Return a known pre-identity launch failure to the prepared state."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        current = now or utc_now()
        with self._transaction():
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
            if (
                str(row["state"]) != ControllerGenerationState.DELIVERY_STARTING.value
                or row["controller_thread_id"] is not None
            ):
                raise StaleWriter("controller generation is not a pre-identity launch")
            decision = (
                self._db()
                .execute(
                    "SELECT revision, state, claimant_kind, claimant_id, claim_token_sha256, claim_lease_expires_at FROM controller_decisions WHERE decision_id = ?",
                    (str(identity),),
                )
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if int(decision["revision"]) != expected_revision:
                raise StaleWriter("controller pre-identity reset authority is stale")
            if claimant_id or token:
                if (
                    decision["claimant_kind"] != ControllerClaimantKind.MODEL.value
                    or decision["claimant_id"] != claimant_id
                    or decision["claim_token_sha256"] != self._controller_claim_token_hash(token)
                    or decision["claim_lease_expires_at"] is None
                    or str(decision["claim_lease_expires_at"]) <= current
                    or str(decision["state"]) != ControllerDecisionState.CLAIMED.value
                ):
                    raise StaleWriter("controller pre-identity reset authority is stale")
            elif (
                str(decision["state"]) != ControllerDecisionState.AWAITING_CLAIM.value
                or decision["claimant_id"] is not None
            ):
                raise StaleWriter("controller pre-identity reset authority is stale")
            # The failed process no longer owns its secret claim capability.
            # Release it with a monotonic revision so a later claimant cannot
            # replay the lost token.
            revision = int(decision["revision"])
            if str(decision["state"]) == ControllerDecisionState.CLAIMED.value:
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'awaiting_claim', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND revision = ?",
                    (revision + 1, current, str(identity), revision),
                )
            self._db().execute(
                "UPDATE controller_decision_generations SET state = 'prepared', updated_at = ? WHERE decision_id = ? AND generation = ? AND state = 'delivery_starting'",
                (current, str(identity), int(generation_value)),
            )
            updated = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            assert updated is not None
            return self._controller_generation_status_from_row(updated)

    def _mark_controller_generation_launch_ambiguous(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        expected_revision: int,
        expected_state: ControllerDecisionState | str,
        expected_claimant_kind: ControllerClaimantKind | str | None,
        expected_claimant_id: str | None,
        now: str | None = None,
    ) -> ControllerDecisionStatus | ProgramControllerDecisionStatus:
        """Close an orphaned controller launch as durable human attention.

        A controller recovery process is reserved in SQLite before ``Popen``.
        If that synchronous launch fails after a restart, there is no reliable
        way to know whether a writer crossed the process boundary.  This
        method records the ambiguous inspection and attention decision in one
        transaction, guarded by the exact decision revision and claimant
        identity observed by the harness.  A newer human claim therefore
        causes the CAS to fail without changing either row.
        """

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        expected_state_value = (
            expected_state.value
            if isinstance(expected_state, ControllerDecisionState)
            else ControllerDecisionState(expected_state).value
        )
        if expected_claimant_kind is None:
            expected_claimant_kind_value: str | None = None
        else:
            expected_claimant_kind_value = (
                expected_claimant_kind.value
                if isinstance(expected_claimant_kind, ControllerClaimantKind)
                else ControllerClaimantKind(expected_claimant_kind).value
            )
        current = now or utc_now()
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if (
                int(decision["current_generation"]) != int(generation_value)
                or int(decision["revision"]) != expected_revision
                or str(decision["state"]) != expected_state_value
                or (str(decision["claimant_kind"]) if decision["claimant_kind"] is not None else None)
                != expected_claimant_kind_value
                or (str(decision["claimant_id"]) if decision["claimant_id"] is not None else None)
                != expected_claimant_id
            ):
                raise StaleWriter("controller launch ambiguity authority is stale")
            generation_row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if generation_row is None:
                raise RecordNotFound(f"controller generation does not exist: {identity}/{generation_value}")
            if (
                str(generation_row["state"])
                not in {
                    ControllerGenerationState.DELIVERY_STARTING.value,
                    ControllerGenerationState.ACTIVE.value,
                }
                or generation_row["inspection_outcome"] is not None
                or generation_row["inspection_completed_at"] is not None
            ):
                raise StaleWriter("controller launch is no longer an orphan")
            updated_generation = self._db().execute(
                "UPDATE controller_decision_generations SET state = 'ambiguous', inspection_started_at = COALESCE(inspection_started_at, ?), inspection_completed_at = ?, inspection_outcome = 'ambiguous', inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND state IN ('delivery_starting','active') AND inspection_outcome IS NULL AND inspection_completed_at IS NULL",
                (current, current, current, current, str(identity), int(generation_value)),
            )
            if updated_generation.rowcount != 1:
                raise StaleWriter("controller launch ambiguity generation CAS is stale")
            updated_decision = self._db().execute(
                "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? "
                "WHERE decision_id = ? AND current_generation = ? AND revision = ? AND state = ?",
                (
                    expected_revision + 1,
                    "controller recovery launch failed before durable identity",
                    current,
                    str(identity),
                    int(generation_value),
                    expected_revision,
                    expected_state_value,
                ),
            )
            if updated_decision.rowcount != 1:
                raise StaleWriter("controller launch ambiguity decision CAS is stale")
            updated = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            assert updated is not None
            if updated["program_id"] is not None:
                return self._program_decision_status_from_row(updated)
            return self._decision_status_from_row(updated)

    def _controller_claim_token_hash(self, token: str) -> str:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise ValueError("controller claim token is invalid")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _controller_completed_inspection_claimant_audit_sha256(
        decision: sqlite3.Row,
        generation: sqlite3.Row,
    ) -> str:
        """Bind a completed read to its exact non-secret model owner facts."""

        if (
            decision["claimant_kind"] != ControllerClaimantKind.MODEL.value
            or decision["claimant_id"] is None
            or decision["claim_token_sha256"] is None
            or decision["claim_started_at"] is None
        ):
            raise CorruptSchemaError("completed controller inspection lacks its model claimant audit")
        audit_revision = int(decision["revision"])
        if generation["inspection_bundle_json"] is not None:
            try:
                inspected_bundle = ModelFacingControllerActionBundle.from_json_bytes(
                    str(generation["inspection_bundle_json"])
                )
            except (TypeError, ValueError) as exc:
                raise CorruptSchemaError("controller recovery inspected bundle is malformed") from exc
            audit_revision = inspected_bundle.expected_revision
        payload = {
            "decision_id": str(decision["decision_id"]),
            "generation": int(generation["generation"]),
            "revision": audit_revision,
            "claimant_kind": str(decision["claimant_kind"]),
            "claimant_id": str(decision["claimant_id"]),
            "claim_token_sha256": str(decision["claim_token_sha256"]),
            "claim_started_at": str(decision["claim_started_at"]),
        }
        return hashlib.sha256(_encode_json(payload).encode("utf-8")).hexdigest()

    def claim_controller_decision(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        claimant_kind: ControllerClaimantKind | str,
        claimant_id: str,
        expected_revision: int,
        generation: int | None = None,
        token: str | None = None,
        lease_seconds: float = 300.0,
        now: str | None = None,
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(str(decision_id))
        kind = (
            claimant_kind
            if isinstance(claimant_kind, ControllerClaimantKind)
            else ControllerClaimantKind(claimant_kind)
        )
        if not isinstance(claimant_id, str) or not claimant_id.strip() or len(claimant_id.encode("utf-8")) > 256:
            raise ValueError("controller claimant id is invalid")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("controller claim revision is invalid")
        if lease_seconds <= 0 or lease_seconds > 3600:
            raise ValueError("controller claim lease is invalid")
        current = now or utc_now()
        with self._transaction():
            row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if generation is not None and int(row["current_generation"]) != generation:
                raise StaleWriter("controller generation is stale")
            if str(row["state"]) in {
                ControllerDecisionState.ACTION_COMMITTED.value,
                ControllerDecisionState.ACKNOWLEDGED.value,
                ControllerDecisionState.SUPERSEDED.value,
                ControllerDecisionState.LEGACY_CLOSED.value,
            } or (
                str(row["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                and row["action_id"] is not None
            ):
                raise StaleWriter("controller decision is terminal or requires human attention")
            inspection = (
                self._db()
                .execute(
                    "SELECT inspection_started_at, inspection_completed_at, inspection_outcome "
                    "FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(row["current_generation"])),
                )
                .fetchone()
            )
            if (
                inspection is not None
                and inspection["inspection_completed_at"] is not None
                and inspection["inspection_outcome"] == ControllerGenerationState.COMPLETED.value
            ):
                # A completed inspection receipt is immutable recovery
                # authority and cannot be taken over through claim replay.
                raise StaleWriter("controller recovery inspection is already completed")
            existing_hash = row["claim_token_sha256"]
            if existing_hash is not None:
                if token is None or self._controller_claim_token_hash(token) != str(existing_hash):
                    raise StaleWriter("controller decision is claimed by another claimant")
                if str(row["claimant_kind"]) != kind.value or str(row["claimant_id"]) != claimant_id:
                    raise StaleWriter("controller claimant identity conflicts")
                if int(row["revision"]) != expected_revision:
                    raise StaleWriter("controller claim revision is stale")
                if row["claim_lease_expires_at"] is not None and str(row["claim_lease_expires_at"]) <= current:
                    raise StaleWriter("controller claim lease has expired")
                return ControllerDecisionClaim(
                    identity,
                    Generation(int(row["current_generation"])),
                    kind,
                    claimant_id,
                    int(row["revision"]),
                    str(row["claim_lease_expires_at"] or current),
                    token,
                )
            if (
                inspection is not None
                and inspection["inspection_started_at"] is not None
                and inspection["inspection_completed_at"] is None
            ):
                raise StaleWriter("controller recovery inspection is already in progress")
            if (
                inspection is not None
                and inspection["inspection_completed_at"] is not None
                and inspection["inspection_outcome"] == ControllerGenerationState.COMPLETED.value
            ):
                # A completed inspection receipt is an immutable recovery
                # capability.  It must be submitted by the recovery path,
                # never replaced by a fresh model or human claim after the
                # original claim lease expires.
                raise StaleWriter("controller recovery inspection is already completed")
            if int(row["revision"]) != expected_revision:
                raise StaleWriter("controller decision revision is stale")
            if token is not None:
                # A capability is minted exactly once by the ledger and
                # returned only in the successful claim response.  Accepting
                # a caller-supplied token for a fresh claim would make an
                # expired/released token indistinguishable from a new claim
                # and permit capability replay.
                raise StaleWriter("controller claim capability cannot be supplied for a fresh claim")
            if str(row["deadline"]) < current:
                raise StaleWriter("controller decision deadline has expired")
            wake = (
                self._db().execute("SELECT state FROM wake_outbox WHERE decision_id = ?", (str(identity),)).fetchone()
            )
            if wake is None:
                raise CorruptSchemaError("controller decision has no wake delivery")
            if str(row["state"]) == ControllerDecisionState.PENDING_DELIVERY.value:
                if kind is not ControllerClaimantKind.HUMAN:
                    raise StaleWriter("model claim requires delivered controller wake")
                if str(wake["state"]) not in {"pending", "not_applicable"}:
                    raise StaleWriter("pending controller wake is no longer claimable")
            elif str(wake["state"]) == "starting":
                raise StaleWriter("controller wake delivery is in its external identity window")
            capability = token or secrets.token_urlsafe(32)
            token_hash = self._controller_claim_token_hash(capability)
            expires = self._expires_after(current, lease_seconds)
            revision = expected_revision + 1
            generation_used = int(row["generation_used"])
            if kind is ControllerClaimantKind.MODEL:
                generation_used = max(generation_used, int(row["current_generation"]))
            self._db().execute(
                "UPDATE controller_decisions SET state = 'claimed', revision = ?, generation_used = ?, claimant_kind = ?, claimant_id = ?, claim_token_sha256 = ?, claim_started_at = ?, claim_lease_expires_at = ?, human_attention_reason = NULL, updated_at = ? WHERE decision_id = ? AND revision = ? AND claim_token_sha256 IS NULL",
                (
                    revision,
                    generation_used,
                    kind.value,
                    claimant_id,
                    token_hash,
                    current,
                    expires,
                    current,
                    str(identity),
                    expected_revision,
                ),
            )
            if kind is ControllerClaimantKind.HUMAN:
                self._db().execute(
                    # Keep the original source route as audit context while
                    # removing any deliverable turn identity.  A suppressed
                    # wake is never deliverable, but its route remains useful
                    # when diagnosing why a human claim won the race.
                    "UPDATE wake_outbox SET state = 'suppressed', source_turn_id = NULL, updated_at = ? WHERE decision_id = ? AND state IN ('pending', 'not_applicable')",
                    (current, str(identity)),
                )
            return ControllerDecisionClaim(
                identity, Generation(int(row["current_generation"])), kind, claimant_id, revision, expires, capability
            )

    def renew_controller_decision_claim(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        claimant_id: str,
        token: str,
        expected_revision: int,
        lease_seconds: float = 300.0,
        now: str | None = None,
    ) -> ControllerDecisionClaim:
        identity = ControllerDecisionId(str(decision_id))
        current = now or utc_now()
        token_hash = self._controller_claim_token_hash(token)
        if lease_seconds <= 0 or lease_seconds > 3600:
            raise ValueError("controller claim lease is invalid")
        with self._transaction():
            row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if row is None or row["claim_token_sha256"] != token_hash or row["claimant_id"] != claimant_id:
                raise StaleWriter("controller claim token is stale")
            if int(row["revision"]) != expected_revision or row["state"] != ControllerDecisionState.CLAIMED.value:
                raise StaleWriter("controller claim revision or state is stale")
            if row["claim_lease_expires_at"] is not None and str(row["claim_lease_expires_at"]) <= current:
                raise StaleWriter("controller claim lease has expired")
            expires = min(self._expires_after(current, lease_seconds), str(row["deadline"]))
            revision = expected_revision + 1
            self._db().execute(
                "UPDATE controller_decisions SET revision = ?, claim_lease_expires_at = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                (revision, expires, current, str(identity), expected_revision),
            )
            return ControllerDecisionClaim(
                identity,
                Generation(int(row["current_generation"])),
                ControllerClaimantKind(str(row["claimant_kind"])),
                claimant_id,
                revision,
                expires,
                token,
            )

    def defer_controller_rate_limit(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        generation: Generation | int,
        claimant_id: str,
        token: str,
        expected_revision: int,
        retry_at: str,
        now: str | None = None,
    ) -> ControllerDecisionClaim:
        """Extend one active model claim to an explicit temporary reset."""

        identity = ControllerDecisionId(str(decision_id))
        generation_value = Generation(generation)
        current = now or utc_now()
        try:
            current_time = datetime.fromisoformat(
                current.removesuffix("Z") + ("+00:00" if current.endswith("Z") else "")
            )
            retry_time = datetime.fromisoformat(
                retry_at.removesuffix("Z") + ("+00:00" if retry_at.endswith("Z") else "")
            )
        except ValueError as exc:
            raise ValueError("controller rate-limit deadline is invalid") from exc
        if current_time.tzinfo is None or retry_time.tzinfo is None:
            raise ValueError("controller rate-limit deadline must be timezone-aware")
        if retry_time <= current_time or retry_time - current_time > timedelta(days=1, seconds=5):
            raise ValueError("controller rate-limit deadline is outside its bound")
        resume_lease = (
            (retry_time + timedelta(minutes=5))
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        extended_deadline = (
            (retry_time + timedelta(hours=1))
            .astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        token_hash = self._controller_claim_token_hash(token)
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            generation_row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(generation_value)),
                )
                .fetchone()
            )
            if decision is None or generation_row is None:
                raise RecordNotFound(f"controller rate-limit decision does not exist: {identity}")
            if (
                int(decision["revision"]) != expected_revision
                or int(decision["current_generation"]) != int(generation_value)
                or decision["state"] != ControllerDecisionState.CLAIMED.value
                or decision["claimant_kind"] != ControllerClaimantKind.MODEL.value
                or decision["claimant_id"] != claimant_id
                or decision["claim_token_sha256"] != token_hash
                or decision["claim_lease_expires_at"] is None
                or str(decision["claim_lease_expires_at"]) <= current
            ):
                raise StaleWriter("controller rate-limit claim is stale")
            if (
                generation_row["state"] != ControllerGenerationState.ACTIVE.value
                or generation_row["controller_thread_id"] is None
                or generation_row["controller_turn_id"] is None
                or generation_row["inspection_started_at"] is not None
            ):
                raise StaleWriter("controller rate-limit generation is not an active bound turn")
            revision = expected_revision + 1
            self._db().execute(
                "UPDATE controller_decisions SET revision = ?, claim_lease_expires_at = ?, "
                "deadline = CASE WHEN deadline < ? THEN ? ELSE deadline END, updated_at = ? "
                "WHERE decision_id = ? AND revision = ?",
                (
                    revision,
                    resume_lease,
                    extended_deadline,
                    extended_deadline,
                    current,
                    str(identity),
                    expected_revision,
                ),
            )
            return ControllerDecisionClaim(
                identity,
                generation_value,
                ControllerClaimantKind.MODEL,
                claimant_id,
                revision,
                resume_lease,
                token,
            )

    def submit_controller_actions(
        self,
        bundle: ModelFacingControllerActionBundle,
        *,
        claimant_id: str,
        token: str,
        allow_recovered: bool = False,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        if not isinstance(bundle, ModelFacingControllerActionBundle):
            raise ValueError("controller action bundle must be typed")
        current = now or utc_now()
        token_hash = self._controller_claim_token_hash(token) if token else None
        if token_hash is None and not allow_recovered:
            raise ValueError("controller claim token is required")
        bundle_json = bundle.to_json_bytes().decode("utf-8")
        digest = bundle.sha256
        with self._transaction():
            # Recovered effects are authorized only by the exact bundle that
            # crossed the one-read inspection boundary.  Perform this check
            # before the idempotent outbox replay branch as well as before a
            # fresh mutation: otherwise a caller could present an invented
            # action id whose prior receipt merely happened to have a
            # completed inspection somewhere in the same generation.
            recovered_generation: sqlite3.Row | None = None
            if allow_recovered:
                recovered_generation = (
                    self._db()
                    .execute(
                        "SELECT inspection_completed_at, inspection_outcome, inspection_bundle_json, inspection_bundle_sha256 "
                        "FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                        (str(bundle.decision_id), int(bundle.generation)),
                    )
                    .fetchone()
                )
                if (
                    recovered_generation is None
                    or recovered_generation["inspection_completed_at"] is None
                    or recovered_generation["inspection_outcome"] != ControllerGenerationState.COMPLETED.value
                    or recovered_generation["inspection_bundle_json"] != bundle_json
                    or recovered_generation["inspection_bundle_sha256"] != digest
                ):
                    raise StaleWriter("controller recovery bundle lacks one exact completed inspection")
            prior = (
                self._db()
                .execute("SELECT * FROM controller_action_outbox WHERE action_id = ?", (bundle.action_id,))
                .fetchone()
            )
            if prior is not None:
                if str(prior["bundle_sha256"]) != digest:
                    raise StaleWriter("controller action id conflicts with a prior bundle")
                if str(prior["decision_id"]) != str(bundle.decision_id):
                    raise StaleWriter("controller action id is bound to another decision")
                decision = (
                    self._db()
                    .execute(
                        "SELECT claim_token_sha256, claimant_id FROM controller_decisions WHERE decision_id = ?",
                        (str(bundle.decision_id),),
                    )
                    .fetchone()
                )
                if decision is None:
                    raise StaleWriter("controller action replay claim is stale")
                if not allow_recovered and (
                    decision["claim_token_sha256"] != token_hash or decision["claimant_id"] != claimant_id
                ):
                    raise StaleWriter("controller action replay claim is stale")
                return ControllerActionReceipt(
                    bundle.action_id,
                    ControllerDecisionId(str(prior["decision_id"])),
                    Generation(int(prior["generation"])),
                    int(prior["expected_revision"]),
                    digest,
                    cast(JsonObject, strict_json_loads(str(prior["effect_receipt_json"]))),
                    str(prior["state"]),
                    str(prior["committed_at"]),
                    str(prior["acknowledged_at"]) if prior["acknowledged_at"] else None,
                )
            row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(bundle.decision_id),))
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller decision does not exist: {bundle.decision_id}")
            if not allow_recovered and (row["claim_token_sha256"] != token_hash or row["claimant_id"] != claimant_id):
                raise StaleWriter("controller claim is stale")
            if allow_recovered:
                # A completed bundle can be recovered after the source
                # process lost its secret claim token.  The inspection CAS is
                # the replacement authority; after reap it is valid from the
                # awaiting state only with the original model audit intact.
                if row["state"] not in {
                    ControllerDecisionState.CLAIMED.value,
                    ControllerDecisionState.AWAITING_CLAIM.value,
                }:
                    raise StaleWriter("controller recovery decision is stale")
                if (
                    row["claimant_kind"] != ControllerClaimantKind.MODEL.value
                    or row["claimant_id"] is None
                    or row["claim_token_sha256"] is None
                    or row["claim_started_at"] is None
                ):
                    raise CorruptSchemaError("controller recovery claimant audit is missing")
            elif row["state"] != ControllerDecisionState.CLAIMED.value:
                raise StaleWriter("controller claim is stale")
            if int(row["revision"]) != bundle.expected_revision or int(row["current_generation"]) != int(
                bundle.generation
            ):
                raise StaleWriter("controller action revision or generation is stale")
            if (
                not allow_recovered
                and row["claim_lease_expires_at"] is not None
                and str(row["claim_lease_expires_at"]) <= current
            ):
                raise StaleWriter("controller claim lease has expired")
            expected_successors = tuple(
                str(item[0])
                for item in self._db()
                .execute(
                    "SELECT successor_dispatch_id FROM authorized_successors WHERE source_dispatch_id = ? AND released_at IS NOT NULL ORDER BY successor_dispatch_id",
                    (str(row["dispatch_id"]),),
                )
                .fetchall()
            )
            if tuple(bundle.expected_successor_dispatch_ids) != expected_successors:
                raise StaleWriter("controller successor snapshot is stale")
            queue = (
                self._db()
                .execute("SELECT * FROM dispatch_queue WHERE dispatch_id = ?", (str(row["dispatch_id"]),))
                .fetchone()
            )
            if queue is None:
                raise CorruptSchemaError("controller decision queue dispatch is missing")
            policy = (
                self._db()
                .execute("SELECT * FROM retry_policies WHERE dispatch_id = ?", (str(row["dispatch_id"]),))
                .fetchone()
            )
            if policy is None:
                raise CorruptSchemaError("controller decision retry policy is missing")
            if str(queue["state"]) in {"completed", "failed", "cancelled", "result_submitted", "finalizing"} and any(
                action.kind not in {ControllerActionKind.ACKNOWLEDGE_ONLY} for action in bundle.actions
            ):
                raise StaleWriter("terminal queue dispatch rejects controller effects")
            recovery_actions = [
                action
                for action in bundle.actions
                if action.kind
                in {
                    ControllerActionKind.RETRY_DISPATCH,
                    ControllerActionKind.CANCEL_DISPATCH,
                    ControllerActionKind.CHANGE_RETRY_BUDGET,
                }
            ]
            if len(recovery_actions) > 1:
                raise StaleWriter("controller recovery actions conflict in one bundle")
            for action in bundle.actions:
                if action.kind in {
                    ControllerActionKind.RETRY_DISPATCH,
                    ControllerActionKind.CANCEL_DISPATCH,
                    ControllerActionKind.CHANGE_RETRY_BUDGET,
                }:
                    if action.expected_retry_revision != int(policy["revision"]):
                        raise StaleWriter("controller retry-policy revision is stale")
                    if action.action_id is None:
                        raise StaleWriter("controller recovery action id is missing")
                    prior_recovery = (
                        self._db()
                        .execute("SELECT * FROM recovery_controls WHERE action_id = ?", (action.action_id,))
                        .fetchone()
                    )
                    if prior_recovery is not None:
                        raise StaleWriter("controller recovery action id already exists")
            effects: list[str] = []
            requires_human_attention = False
            human_attention_reason: str | None = None
            for action in bundle.actions:
                if action.dispatch_id is not None and str(action.dispatch_id) != str(row["dispatch_id"]):
                    raise StaleWriter("controller action targets another dispatch")
                effects.append(action.kind.value)
                if action.kind is ControllerActionKind.REQUIRE_HUMAN_ATTENTION:
                    requires_human_attention = True
                    human_attention_reason = action.reason
                elif action.kind is ControllerActionKind.REARM_CHECKPOINT:
                    binding = (
                        self._db()
                        .execute(
                            "SELECT checkpoint_armed FROM queue_bindings WHERE dispatch_id = ?",
                            (str(row["dispatch_id"]),),
                        )
                        .fetchone()
                    )
                    if binding is None:
                        raise CorruptSchemaError("controller checkpoint binding is missing")
                    self._db().execute(
                        "UPDATE queue_bindings SET checkpoint_deadline = ?, checkpoint_armed = 1, updated_at = ? WHERE dispatch_id = ?",
                        (
                            self._expires_after(current, action.checkpoint_seconds or 1800),
                            current,
                            str(row["dispatch_id"]),
                        ),
                    )
                elif action.kind is ControllerActionKind.CHANGE_RETRY_BUDGET:
                    assert action.requested_retry_budget is not None
                    used = {
                        "pre_identity_budget": int(policy["pre_identity_used"]),
                        "invalid_chain_budget": int(policy["invalid_chain_used"]),
                        "schema_envelope_budget": int(policy["schema_envelope_used"]),
                        "post_identity_loss_budget": int(policy["post_identity_loss_used"]),
                    }
                    requested = action.requested_retry_budget
                    if any(value < used[name] for name, value in requested.to_json().items()):
                        raise StaleWriter("controller retry budget cannot fall below consumed retries")
                    self._db().execute(
                        "UPDATE retry_policies SET revision = revision + 1, pre_identity_budget = ?, invalid_chain_budget = ?, schema_envelope_budget = ?, post_identity_loss_budget = ?, updated_at = ? WHERE dispatch_id = ? AND revision = ?",
                        (
                            requested.pre_identity_budget,
                            requested.invalid_chain_budget,
                            requested.schema_envelope_budget,
                            requested.post_identity_loss_budget,
                            current,
                            str(row["dispatch_id"]),
                            action.expected_retry_revision,
                        ),
                    )
                    self._db().execute(
                        "INSERT INTO recovery_controls(action_id, dispatch_id, expected_revision, action_kind, reason, requested_budget_json, applied_revision, created_at) VALUES (?, ?, ?, 'budget_change', ?, ?, ?, ?)",
                        (
                            action.action_id,
                            str(row["dispatch_id"]),
                            action.expected_retry_revision,
                            action.reason or "controller budget change",
                            _encode_json(requested.to_json()),
                            action.expected_retry_revision + 1,
                            current,
                        ),
                    )
                elif action.kind is ControllerActionKind.RETRY_DISPATCH:
                    if str(queue["state"]) != "human_attention_required":
                        raise StaleWriter("controller retry requires human-attention state")
                    self._db().execute(
                        "UPDATE retry_policies SET revision = revision + 1, strategy = 'none', human_attention_reason = NULL, next_eligible_at = ?, updated_at = ? WHERE dispatch_id = ? AND revision = ?",
                        (current, current, str(row["dispatch_id"]), action.expected_retry_revision),
                    )
                    self._db().execute(
                        "DELETE FROM attempt_capabilities WHERE dispatch_id = ?", (str(row["dispatch_id"]),)
                    )
                    self._db().execute("DELETE FROM worker_liveness WHERE dispatch_id = ?", (str(row["dispatch_id"]),))
                    self._db().execute(
                        "UPDATE recovery_state SET recovery_state = 'none', next_eligible_at = NULL, human_attention_reason = NULL, updated_at = ? WHERE dispatch_id = ?",
                        (current, str(row["dispatch_id"])),
                    )
                    self._db().execute(
                        "UPDATE dispatch_queue SET state = 'queued', claim_epoch = NULL, claim_nonce_sha256 = NULL, attempt = attempt + 1, thread_id = NULL, host_id = NULL, available_at = ?, updated_at = ? WHERE dispatch_id = ?",
                        (current, current, str(row["dispatch_id"])),
                    )
                    self._db().execute(
                        "INSERT INTO recovery_controls(action_id, dispatch_id, expected_revision, action_kind, reason, requested_budget_json, applied_revision, created_at) VALUES (?, ?, ?, 'retry', ?, NULL, ?, ?)",
                        (
                            action.action_id,
                            str(row["dispatch_id"]),
                            action.expected_retry_revision,
                            action.reason or "controller retry",
                            action.expected_retry_revision + 1,
                            current,
                        ),
                    )
                elif action.kind is ControllerActionKind.CANCEL_DISPATCH:
                    self._cancel_queue_row_in_transaction(queue)
                    self._db().execute(
                        "INSERT INTO recovery_controls(action_id, dispatch_id, expected_revision, action_kind, reason, requested_budget_json, applied_revision, created_at) VALUES (?, ?, ?, 'cancel', ?, NULL, ?, ?)",
                        (
                            action.action_id,
                            str(row["dispatch_id"]),
                            action.expected_retry_revision,
                            action.reason or "controller cancellation",
                            action.expected_retry_revision + 1,
                            current,
                        ),
                    )
            revision = int(row["revision"]) + 1
            receipt_payload: JsonObject = {
                "action_id": bundle.action_id,
                "decision_id": str(bundle.decision_id),
                "generation": int(bundle.generation),
                "bundle_sha256": digest,
                "applied_actions": effects,
                "applied_revision": revision,
            }
            receipt_json = _encode_json(receipt_payload)
            receipt_digest = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
            if row["claimant_kind"] is None or row["claimant_id"] is None:
                raise CorruptSchemaError("controller action claimant audit is missing")
            outbox_claimant_kind = str(row["claimant_kind"])
            outbox_claimant_id = str(row["claimant_id"])
            self._db().execute(
                "INSERT INTO controller_action_outbox(action_id, decision_id, generation, expected_revision, claimant_kind, claimant_id, bundle_json, bundle_sha256, effect_receipt_json, effect_receipt_sha256, state, committed_at, acknowledged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, NULL)",
                (
                    bundle.action_id,
                    str(bundle.decision_id),
                    int(bundle.generation),
                    bundle.expected_revision,
                    outbox_claimant_kind,
                    outbox_claimant_id,
                    bundle_json,
                    digest,
                    receipt_json,
                    receipt_digest,
                    current,
                ),
            )
            next_decision_state = (
                ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                if requires_human_attention
                else ControllerDecisionState.ACTION_COMMITTED.value
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = ?, revision = ?, action_id = ?, action_bundle_json = ?, action_bundle_sha256 = ?, committed_at = ?, human_attention_reason = ?, claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND revision = ?",
                (
                    next_decision_state,
                    revision,
                    bundle.action_id,
                    bundle_json,
                    digest,
                    current,
                    human_attention_reason,
                    current,
                    str(bundle.decision_id),
                    bundle.expected_revision,
                ),
            )
            return ControllerActionReceipt(
                bundle.action_id,
                bundle.decision_id,
                bundle.generation,
                bundle.expected_revision,
                digest,
                receipt_payload,
                "committed",
                current,
            )

    def submit_recovered_controller_actions(
        self,
        decision_or_bundle: ControllerDecisionId | str | ModelFacingControllerActionBundle,
        *,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        """Commit only the canonical bundle persisted by inspection.

        The normal recovery call names a decision, never a caller-authored
        bundle.  A typed bundle is accepted solely as a compatibility
        assertion and must byte-for-byte match the durable inspection receipt.
        """

        supplied = decision_or_bundle if isinstance(decision_or_bundle, ModelFacingControllerActionBundle) else None
        identity = supplied.decision_id if supplied is not None else ControllerDecisionId(str(decision_or_bundle))
        row = (
            self._db()
            .execute(
                "SELECT inspection_bundle_json, inspection_bundle_sha256 FROM controller_decision_generations "
                "WHERE decision_id = (SELECT decision_id FROM controller_decisions WHERE decision_id = ?) "
                "AND generation = (SELECT current_generation FROM controller_decisions WHERE decision_id = ?)",
                (str(identity), str(identity)),
            )
            .fetchone()
        )
        if row is None or row["inspection_bundle_json"] is None or row["inspection_bundle_sha256"] is None:
            raise StaleWriter("controller recovery has no persisted inspected bundle")
        try:
            persisted = ModelFacingControllerActionBundle.from_json_bytes(str(row["inspection_bundle_json"]))
        except (TypeError, ValueError) as exc:
            raise CorruptSchemaError("controller recovery inspected bundle is malformed") from exc
        if hashlib.sha256(str(row["inspection_bundle_json"]).encode("utf-8")).hexdigest() != str(
            row["inspection_bundle_sha256"]
        ) or persisted.sha256 != str(row["inspection_bundle_sha256"]):
            raise CorruptSchemaError("controller recovery inspected bundle digest is invalid")
        if supplied is not None and supplied.sha256 != persisted.sha256:
            raise StaleWriter("controller recovery bundle is not the persisted inspected bundle")
        return self.submit_controller_actions(
            persisted,
            claimant_id="recovery-inspection",
            token="",
            allow_recovered=True,
            now=now,
        )

    def submit_recovered_program_controller_actions(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        """Commit the exact program bundle retained by one recovery read."""

        identity = ControllerDecisionId(str(decision_id))
        row = (
            self._db()
            .execute(
                "SELECT inspection_completed_at, inspection_outcome, inspection_bundle_json, inspection_bundle_sha256 "
                "FROM controller_decision_generations "
                "WHERE decision_id = ? AND generation = (SELECT current_generation FROM controller_decisions WHERE decision_id = ?)",
                (str(identity), str(identity)),
            )
            .fetchone()
        )
        if (
            row is None
            or row["inspection_completed_at"] is None
            or row["inspection_outcome"] != ControllerGenerationState.COMPLETED.value
            or row["inspection_bundle_json"] is None
            or row["inspection_bundle_sha256"] is None
        ):
            raise StaleWriter("program controller recovery has no persisted inspected bundle")
        try:
            persisted = ModelFacingProgramControllerActionBundle.from_json_bytes(str(row["inspection_bundle_json"]))
        except (TypeError, ValueError) as exc:
            raise CorruptSchemaError("program controller recovery bundle is malformed") from exc
        if hashlib.sha256(str(row["inspection_bundle_json"]).encode("utf-8")).hexdigest() != str(
            row["inspection_bundle_sha256"]
        ) or persisted.sha256 != str(row["inspection_bundle_sha256"]):
            raise CorruptSchemaError("program controller recovery bundle digest is invalid")
        return self.submit_program_controller_actions(
            persisted,
            claimant_id="recovery-inspection",
            token="",
            allow_recovered=True,
            now=now,
        )

    def acknowledge_controller_action(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        claimant_id: str,
        token: str,
        allow_recovered: bool = False,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        identity = ControllerDecisionId(str(decision_id))
        current = now or utc_now()
        token_hash = self._controller_claim_token_hash(token) if token else None
        if token_hash is None and not allow_recovered:
            raise ValueError("controller claim token is required")
        with self._transaction():
            row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            outbox = (
                self._db()
                .execute("SELECT * FROM controller_action_outbox WHERE action_id = ?", (action_id,))
                .fetchone()
            )
            if outbox is None or outbox["decision_id"] != str(identity) or outbox["bundle_sha256"] != bundle_sha256:
                raise StaleWriter("controller action receipt is stale")
            if row["program_id"] is not None:
                program_bundle = ModelFacingProgramControllerActionBundle.from_json_bytes(str(outbox["bundle_json"]))
                incomplete = [
                    effect_id
                    for effect_id, _milestone_id in program_bundle.external_effects
                    if self.program_action_effect_state(action_id, effect_id) == "pending"
                ]
                if incomplete:
                    raise StaleWriter("program controller action has nonterminal external effects")
            if allow_recovered:
                # A committed outbox effect is independently recoverable: a
                # source process may have crashed after the atomic commit but
                # before its acknowledgement RPC.  In that state no second
                # SDK inspection is authorized.  For an uncommitted decision,
                # the dedicated recovered path still requires the one
                # completed authoritative inspection that supplied its
                # bundle.
                committed_effect = str(row["action_id"]) == action_id and str(row["state"]) in {
                    ControllerDecisionState.ACTION_COMMITTED.value,
                    ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value,
                }
                if not committed_effect:
                    inspected = (
                        self._db()
                        .execute(
                            "SELECT inspection_completed_at, inspection_outcome FROM controller_decision_generations "
                            "WHERE decision_id = ? AND generation = ?",
                            (str(identity), int(outbox["generation"])),
                        )
                        .fetchone()
                    )
                    if (
                        inspected is None
                        or inspected["inspection_completed_at"] is None
                        or inspected["inspection_outcome"] != ControllerGenerationState.COMPLETED.value
                    ):
                        raise StaleWriter("controller recovery acknowledgement lacks one completed inspection")
            if outbox["state"] == "acknowledged":
                if not allow_recovered and (
                    row["claim_token_sha256"] != token_hash or row["claimant_id"] != claimant_id
                ):
                    raise StaleWriter("controller acknowledgement replay claim is stale")
                if int(row["revision"]) != committed_revision:
                    raise StaleWriter("controller acknowledgement replay revision is stale")
                return ControllerActionReceipt(
                    action_id,
                    identity,
                    Generation(int(outbox["generation"])),
                    int(outbox["expected_revision"]),
                    str(outbox["bundle_sha256"]),
                    cast(JsonObject, strict_json_loads(str(outbox["effect_receipt_json"]))),
                    "acknowledged",
                    str(outbox["committed_at"]),
                    str(outbox["acknowledged_at"]),
                )
            if not allow_recovered and (row["claim_token_sha256"] != token_hash or row["claimant_id"] != claimant_id):
                raise StaleWriter("controller acknowledgement claim or revision is stale")
            if int(row["revision"]) != committed_revision:
                raise StaleWriter("controller acknowledgement claim or revision is stale")
            self._db().execute(
                "UPDATE controller_action_outbox SET state = 'acknowledged', acknowledged_at = ? WHERE action_id = ? AND state = 'committed'",
                (current, action_id),
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = CASE WHEN state = 'human_attention_required' THEN 'human_attention_required' ELSE 'acknowledged' END, acknowledged_at = ?, claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND state IN ('action_committed', 'human_attention_required')",
                (current, current, str(identity)),
            )
            return ControllerActionReceipt(
                action_id,
                identity,
                Generation(int(outbox["generation"])),
                int(outbox["expected_revision"]),
                str(outbox["bundle_sha256"]),
                cast(JsonObject, strict_json_loads(str(outbox["effect_receipt_json"]))),
                "acknowledged",
                str(outbox["committed_at"]),
                current,
            )

    def acknowledge_recovered_controller_action(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        action_id: str,
        bundle_sha256: str,
        committed_revision: int,
        now: str | None = None,
    ) -> ControllerActionReceipt:
        """Acknowledge a bundle committed by the recovery read authority."""

        return self.acknowledge_controller_action(
            decision_id,
            action_id=action_id,
            bundle_sha256=bundle_sha256,
            committed_revision=committed_revision,
            claimant_id="recovery-inspection",
            token="",
            allow_recovered=True,
            now=now,
        )

    @staticmethod
    def _controller_generation_status_from_row(row: sqlite3.Row) -> ControllerGenerationStatus:
        return ControllerGenerationStatus(
            ControllerDecisionId(str(row["decision_id"])),
            Generation(int(row["generation"])),
            str(row["lineage_id"]),
            ControllerGenerationState(str(row["state"])),
            Generation(int(row["predecessor_generation"])) if row["predecessor_generation"] is not None else None,
            str(row["source_kind"]),
            str(row["prompt_sha256"]) if row["prompt_sha256"] is not None else None,
            ThreadIdentity(str(row["controller_thread_id"])) if row["controller_thread_id"] else None,
            str(row["controller_turn_id"]) if row["controller_turn_id"] else None,
            str(row["inspection_outcome"]) if row["inspection_outcome"] is not None else None,
        )

    def reserve_controller_recovery_inspection(
        self, decision_id: ControllerDecisionId | str, *, now: str | None = None
    ) -> ControllerRecoveryInspectionClaim:
        """CAS-reserve the one authoritative SDK read for a generation."""

        identity = ControllerDecisionId(str(decision_id))
        current = now or utc_now()
        # Expiring a crashed reservation is itself a durable transition.  Do
        # it in a completed transaction before entering the reservation CAS;
        # raising from inside ``_transaction`` would roll the attention fact
        # back and strand every restart behind the stale lease.
        expired = False
        with self._transaction():
            decision_row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            generation_row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(decision_row["current_generation"])) if decision_row is not None else ("", 0),
                )
                .fetchone()
                if decision_row is not None
                else None
            )
            if (
                decision_row is not None
                and generation_row is not None
                and generation_row["inspection_started_at"] is not None
                and generation_row["inspection_completed_at"] is None
                and generation_row["inspection_lease_expires_at"] is not None
                and str(generation_row["inspection_lease_expires_at"]) <= current
            ):
                revision = int(decision_row["revision"]) + 1
                self._db().execute(
                    "UPDATE controller_decision_generations SET state = 'ambiguous', "
                    "inspection_completed_at = ?, inspection_outcome = 'ambiguous', "
                    "inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? "
                    "WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                    (current, current, current, str(identity), int(generation_row["generation"])),
                )
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, "
                    "claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, "
                    "claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, "
                    "updated_at = ? WHERE decision_id = ? AND revision = ?",
                    (
                        revision,
                        "controller inspection lease expired",
                        current,
                        str(identity),
                        int(decision_row["revision"]),
                    ),
                )
                expired = True
        if expired:
            raise StaleWriter("controller recovery inspection lease has expired")
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if str(decision["state"]) in {
                ControllerDecisionState.ACTION_COMMITTED.value,
                ControllerDecisionState.ACKNOWLEDGED.value,
                ControllerDecisionState.SUPERSEDED.value,
                ControllerDecisionState.LEGACY_CLOSED.value,
            } or (
                str(decision["state"]) == ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                and decision["action_id"] is not None
            ):
                raise StaleWriter("controller decision is terminal")
            if str(decision["state"]) not in {
                ControllerDecisionState.AWAITING_CLAIM.value,
                ControllerDecisionState.CLAIMED.value,
            }:
                raise StaleWriter("controller recovery inspection requires a claimable decision")
            if (
                str(decision["state"]) == ControllerDecisionState.CLAIMED.value
                and decision["claimant_kind"] == ControllerClaimantKind.HUMAN.value
            ):
                raise StaleWriter("human controller claim owns the decision")
            generation = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(decision["current_generation"])),
                )
                .fetchone()
            )
            if generation is None:
                raise CorruptSchemaError("controller decision generation is missing")
            if generation["inspection_started_at"] is not None:
                if (
                    generation["inspection_completed_at"] is None
                    and generation["inspection_lease_expires_at"] is not None
                    and str(generation["inspection_lease_expires_at"]) <= current
                ):
                    revision = int(decision["revision"]) + 1
                    self._db().execute(
                        "UPDATE controller_decision_generations SET state = 'ambiguous', "
                        "inspection_completed_at = ?, inspection_outcome = 'ambiguous', "
                        "inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? "
                        "WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                        (current, current, current, str(identity), int(generation["generation"])),
                    )
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, "
                        "claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, "
                        "claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, "
                        "updated_at = ? WHERE decision_id = ? AND revision = ?",
                        (
                            revision,
                            "controller inspection lease expired",
                            current,
                            str(identity),
                            int(decision["revision"]),
                        ),
                    )
                    raise StaleWriter("controller recovery inspection lease has expired")
                raise StaleWriter("controller recovery inspection already consumed")
            token = secrets.token_urlsafe(32)
            token_hash = self._controller_claim_token_hash(token)
            lease_expires = min(self._expires_after(current, 300), str(decision["deadline"]))
            updated = self._db().execute(
                "UPDATE controller_decision_generations SET inspection_started_at = ?, inspection_token_sha256 = ?, inspection_lease_expires_at = ?, updated_at = ? "
                "WHERE decision_id = ? AND generation = ? AND inspection_started_at IS NULL",
                (current, token_hash, lease_expires, current, str(identity), int(decision["current_generation"])),
            )
            if updated.rowcount != 1:
                raise StaleWriter("controller recovery inspection reservation is stale")
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(decision["current_generation"])),
                )
                .fetchone()
            )
            assert row is not None
            return ControllerRecoveryInspectionClaim(
                identity,
                Generation(int(decision["current_generation"])),
                int(decision["revision"]),
                lease_expires,
                token,
            )

    def complete_controller_recovery_inspection(
        self,
        decision_id: ControllerDecisionId | str,
        *,
        inspection_outcome: str,
        claim: ControllerRecoveryInspectionClaim | None = None,
        token: str | None = None,
        bundle: ModelFacingControllerActionBundle | ModelFacingProgramControllerActionBundle | None = None,
        now: str | None = None,
    ) -> ControllerGenerationStatus:
        """Persist one SDK inspection and, when safe, roll to generation two."""

        identity = ControllerDecisionId(str(decision_id))
        current = now or utc_now()
        if claim is not None and not isinstance(claim, ControllerRecoveryInspectionClaim):
            raise ValueError("controller recovery inspection claim must be typed")
        if claim is not None and token is not None and token != claim.token:
            raise StaleWriter("controller recovery inspection token conflicts")
        if inspection_outcome == "malformed":
            outcome = ControllerGenerationState.AMBIGUOUS
        else:
            try:
                outcome = ControllerGenerationState(inspection_outcome)
            except ValueError as exc:
                raise ValueError("controller inspection outcome is invalid") from exc
        if outcome is ControllerGenerationState.DELIVERY_STARTING:
            outcome = ControllerGenerationState.AMBIGUOUS
        with self._transaction():
            decision = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if decision is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            if str(decision["state"]) not in {
                ControllerDecisionState.AWAITING_CLAIM.value,
                ControllerDecisionState.CLAIMED.value,
            }:
                raise StaleWriter("controller recovery inspection decision is stale")
            if (
                str(decision["state"]) == ControllerDecisionState.CLAIMED.value
                and decision["claimant_kind"] == ControllerClaimantKind.HUMAN.value
            ):
                raise StaleWriter("human controller claim owns the decision")
            generation = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(decision["current_generation"])),
                )
                .fetchone()
            )
            if generation is None:
                raise CorruptSchemaError("controller decision generation is missing")
            if generation["inspection_started_at"] is None or generation["inspection_completed_at"] is not None:
                raise StaleWriter("controller recovery inspection is not pending")
            if claim is None and token is not None:
                claim = ControllerRecoveryInspectionClaim(
                    identity,
                    Generation(int(generation["generation"])),
                    int(decision["revision"]),
                    str(generation["inspection_lease_expires_at"] or current),
                    token,
                )
            if claim is None:
                raise StaleWriter("controller recovery inspection claim is required")
            if (
                claim.decision_id != identity
                or int(claim.generation) != int(generation["generation"])
                or int(claim.revision) != int(decision["revision"])
                or generation["inspection_token_sha256"] != self._controller_claim_token_hash(claim.token)
                or generation["inspection_lease_expires_at"] is None
                or str(generation["inspection_lease_expires_at"]) <= current
            ):
                raise StaleWriter("controller recovery inspection claim is stale or expired")
            if outcome is ControllerGenerationState.COMPLETED:
                if not isinstance(bundle, ModelFacingControllerActionBundle | ModelFacingProgramControllerActionBundle):
                    raise ValueError("completed controller inspection requires its typed action bundle")
                if bundle.decision_id != identity or int(bundle.generation) != int(generation["generation"]):
                    raise StaleWriter("controller inspection bundle identity is stale")
                bundle_revision = (
                    bundle.expected_revision
                    if isinstance(bundle, ModelFacingControllerActionBundle)
                    else bundle.expected_program_revision
                )
                if int(bundle_revision) != int(decision["revision"]):
                    raise StaleWriter("controller inspection bundle revision is stale")
                bundle_json = bundle.to_json_bytes().decode("utf-8")
                bundle_sha256 = bundle.sha256
                inspection_audit_sha256 = self._controller_completed_inspection_claimant_audit_sha256(
                    decision,
                    generation,
                )
            elif bundle is not None:
                raise ValueError("non-completed controller inspection cannot carry an action bundle")
            else:
                bundle_json = None
                bundle_sha256 = None
                inspection_audit_sha256 = str(generation["inspection_token_sha256"])
            self._db().execute(
                "UPDATE controller_decision_generations SET inspection_completed_at = ?, inspection_outcome = ?, state = ?, inspection_token_sha256 = ?, inspection_bundle_json = ?, inspection_bundle_sha256 = ?, inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                (
                    current,
                    outcome.value,
                    outcome.value,
                    inspection_audit_sha256,
                    bundle_json,
                    bundle_sha256,
                    current
                    if outcome
                    in {
                        ControllerGenerationState.COMPLETED,
                        ControllerGenerationState.FAILED,
                        ControllerGenerationState.INTERRUPTED,
                        ControllerGenerationState.UNAVAILABLE,
                        ControllerGenerationState.AMBIGUOUS,
                    }
                    else None,
                    current,
                    str(identity),
                    int(decision["current_generation"]),
                ),
            )
            if outcome is ControllerGenerationState.ACTIVE:
                row = (
                    self._db()
                    .execute(
                        "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                        (str(identity), int(decision["current_generation"])),
                    )
                    .fetchone()
                )
                assert row is not None
                return self._controller_generation_status_from_row(row)

            # A completed generation may carry a valid action bundle produced
            # under its original claim.  Preserve that CAS revision until the
            # harness submits the inspected bundle; the secret token remains
            # only as a digest.  Terminal failures and replacements clear the
            # lost claim and advance the decision revision.
            next_generation = int(decision["current_generation"])
            budget = int(decision["generation_budget"])
            claim_expired_or_unclaimed = (
                decision["claim_token_sha256"] is None
                or decision["claim_lease_expires_at"] is None
                or str(decision["claim_lease_expires_at"]) <= current
            )
            can_replace = (
                outcome
                in {
                    ControllerGenerationState.FAILED,
                    ControllerGenerationState.INTERRUPTED,
                    ControllerGenerationState.UNAVAILABLE,
                }
                and next_generation < budget
                and (outcome is ControllerGenerationState.UNAVAILABLE or claim_expired_or_unclaimed)
            )
            if outcome is ControllerGenerationState.COMPLETED:
                next_state = (
                    ControllerDecisionState.CLAIMED.value
                    if decision["claimant_kind"] is not None
                    else ControllerDecisionState.AWAITING_CLAIM.value
                )
            elif can_replace:
                next_generation += 1
                next_state = ControllerDecisionState.AWAITING_CLAIM.value
            elif outcome in {
                ControllerGenerationState.FAILED,
                ControllerGenerationState.INTERRUPTED,
                ControllerGenerationState.UNAVAILABLE,
                ControllerGenerationState.AMBIGUOUS,
            }:
                next_state = ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
            else:
                next_state = ControllerDecisionState.AWAITING_CLAIM.value
            revision = (
                int(decision["revision"])
                if outcome is ControllerGenerationState.COMPLETED
                else int(decision["revision"]) + 1
            )
            reason = (
                None
                if next_state != ControllerDecisionState.HUMAN_ATTENTION_REQUIRED.value
                else f"controller inspection: {outcome.value}"
            )
            if outcome is ControllerGenerationState.COMPLETED:
                self._db().execute(
                    "UPDATE controller_decisions SET state = ?, revision = ?, current_generation = ?, human_attention_reason = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                    (next_state, revision, next_generation, reason, current, str(identity), int(decision["revision"])),
                )
            else:
                self._db().execute(
                    "UPDATE controller_decisions SET state = ?, revision = ?, current_generation = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                    (next_state, revision, next_generation, reason, current, str(identity), int(decision["revision"])),
                )
            if can_replace:
                self._db().execute(
                    "INSERT INTO controller_decision_generations(decision_id, generation, lineage_id, predecessor_generation, source_kind, state, prompt_sha256, controller_thread_id, controller_turn_id, inspection_started_at, inspection_token_sha256, inspection_lease_expires_at, inspection_completed_at, inspection_outcome, inspection_bundle_json, inspection_bundle_sha256, started_at, terminal_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'replacement_controller', 'prepared', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                    (
                        str(identity),
                        next_generation,
                        str(generation["lineage_id"]),
                        int(generation["generation"]),
                        current,
                        current,
                    ),
                )
            row = (
                self._db()
                .execute(
                    "SELECT * FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                    (str(identity), next_generation),
                )
                .fetchone()
            )
            assert row is not None
            return self._controller_generation_status_from_row(row)

    def reap_controller_claim(
        self, decision_id: ControllerDecisionId | str, *, now: str | None = None
    ) -> ControllerDecisionStatus:
        """Release only an expired human claim (or an inspected model claim)."""

        identity = ControllerDecisionId(str(decision_id))
        current = now or utc_now()
        with self._transaction():
            row = (
                self._db()
                .execute("SELECT * FROM controller_decisions WHERE decision_id = ?", (str(identity),))
                .fetchone()
            )
            if row is None:
                raise RecordNotFound(f"controller decision does not exist: {identity}")
            expires = row["claim_lease_expires_at"]
            generation = (
                self._db()
                .execute(
                    "SELECT inspection_started_at, inspection_completed_at, inspection_lease_expires_at, "
                    "inspection_outcome, inspection_bundle_json, inspection_bundle_sha256, state "
                    "FROM controller_decision_generations "
                    "WHERE decision_id = ? AND generation = ?",
                    (str(identity), int(row["current_generation"])),
                )
                .fetchone()
            )
            if generation is None:
                raise CorruptSchemaError("controller decision generation is missing")
            if (
                row["action_id"] is None
                and generation["inspection_started_at"] is not None
                and generation["inspection_completed_at"] is None
            ):
                lease = generation["inspection_lease_expires_at"]
                if lease is not None and str(lease) <= current:
                    revision = int(row["revision"]) + 1
                    self._db().execute(
                        "UPDATE controller_decision_generations SET state = 'ambiguous', "
                        "inspection_completed_at = ?, inspection_outcome = 'ambiguous', "
                        "inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? "
                        "WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                        (current, current, current, str(identity), int(row["current_generation"])),
                    )
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, "
                        "claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, "
                        "claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, "
                        "updated_at = ? WHERE decision_id = ? AND revision = ?",
                        (
                            revision,
                            "controller inspection lease expired",
                            current,
                            str(identity),
                            int(row["revision"]),
                        ),
                    )
                    return self.controller_decision(identity)
                if lease is not None and str(lease) > current:
                    # A live read reservation owns the decision until it is
                    # completed, even if its claimant lease has elapsed.
                    return self._decision_status_from_row(row)
            if (
                row["action_id"] is None
                and generation["inspection_completed_at"] is not None
                and generation["inspection_outcome"] == ControllerGenerationState.COMPLETED.value
                and generation["inspection_bundle_json"] is not None
                and generation["inspection_bundle_sha256"] is not None
                and row["claimant_kind"] == ControllerClaimantKind.MODEL.value
                and expires is not None
                and str(expires) <= current
            ):
                # The completed inspection receipt is the durable recovery
                # authority.  Deactivate the expired lease while preserving
                # the original non-secret claimant audit tuple and revision
                # (which is embedded in the bundle).  The completed receipt
                # rejects both capability replay and claimant takeover.
                released = self._db().execute(
                    "UPDATE controller_decisions SET state = 'awaiting_claim', "
                    "claim_lease_expires_at = NULL, human_attention_reason = NULL, updated_at = ? "
                    "WHERE decision_id = ? AND revision = ? AND state = 'claimed' "
                    "AND claimant_kind = 'model' AND action_id IS NULL",
                    (current, str(identity), int(row["revision"])),
                )
                if released.rowcount != 1:
                    raise StaleWriter("controller completed inspection lease release is stale")
                return self.controller_decision(identity)
            if row["claimant_kind"] == ControllerClaimantKind.MODEL.value:
                inspection = (
                    self._db()
                    .execute(
                        "SELECT inspection_started_at, inspection_completed_at, inspection_lease_expires_at FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                        (str(identity), int(row["current_generation"])),
                    )
                    .fetchone()
                )
                if (
                    inspection is not None
                    and inspection["inspection_started_at"] is not None
                    and inspection["inspection_completed_at"] is None
                    and inspection["inspection_lease_expires_at"] is not None
                    and str(inspection["inspection_lease_expires_at"]) <= current
                ):
                    revision = int(row["revision"]) + 1
                    self._db().execute(
                        "UPDATE controller_decision_generations SET state = 'ambiguous', inspection_completed_at = ?, inspection_outcome = 'ambiguous', inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                        (current, current, current, str(identity), int(row["current_generation"])),
                    )
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                        (revision, "controller inspection lease expired", current, str(identity), int(row["revision"])),
                    )
                    return self.controller_decision(identity)
            if row["claim_token_sha256"] is None or expires is None or str(expires) > current:
                return self._decision_status_from_row(row)
            if row["claimant_kind"] == ControllerClaimantKind.MODEL.value:
                generation = (
                    self._db()
                    .execute(
                        "SELECT inspection_started_at, inspection_completed_at, inspection_outcome, inspection_lease_expires_at, state FROM controller_decision_generations WHERE decision_id = ? AND generation = ?",
                        (str(identity), int(row["current_generation"])),
                    )
                    .fetchone()
                )
                if generation is None:
                    raise CorruptSchemaError("controller decision generation is missing")
                if generation["inspection_started_at"] is not None and generation["inspection_completed_at"] is None:
                    lease = generation["inspection_lease_expires_at"]
                    if lease is not None and str(lease) > current:
                        # The read capability is still live; its owner keeps
                        # the decision lease until the inspection completes.
                        return self._decision_status_from_row(row)
                    revision = int(row["revision"]) + 1
                    self._db().execute(
                        "UPDATE controller_decision_generations SET state = 'ambiguous', inspection_completed_at = ?, inspection_outcome = 'ambiguous', inspection_lease_expires_at = NULL, terminal_at = ?, updated_at = ? WHERE decision_id = ? AND generation = ? AND inspection_completed_at IS NULL",
                        (current, current, current, str(identity), int(row["current_generation"])),
                    )
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                        (revision, "controller inspection lease expired", current, str(identity), int(row["revision"])),
                    )
                    return self.controller_decision(identity)
                # An authoritative active-writer inspection means the source
                # controller may still own the shared SDK thread.  Expiry
                # alone must not turn that live generation into a fresh model
                # launch.  Close the decision for human attention while
                # preserving the active generation lineage; a human may then
                # claim the same typed CAS boundary if intervention is needed.
                if generation["inspection_outcome"] == ControllerGenerationState.ACTIVE.value:
                    revision = int(row["revision"]) + 1
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'human_attention_required', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, human_attention_reason = ?, updated_at = ? WHERE decision_id = ? AND revision = ?",
                        (
                            revision,
                            "active controller generation requires human attention",
                            current,
                            str(identity),
                            int(row["revision"]),
                        ),
                    )
                    return self.controller_decision(identity)
                if generation["inspection_outcome"] is None:
                    # Clock expiry alone never releases a model claim.  The
                    # persisted generation must first cross the one-read
                    # inspection boundary (or become an explicit ambiguous
                    # human-attention fact).
                    return self._decision_status_from_row(row)
            revision = int(row["revision"]) + 1
            self._db().execute(
                "UPDATE controller_decisions SET state = 'awaiting_claim', revision = ?, claimant_kind = NULL, claimant_id = NULL, claim_token_sha256 = NULL, claim_started_at = NULL, claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND revision = ?",
                (revision, current, str(identity), int(row["revision"])),
            )
            return self.controller_decision(identity)

    def request_controller_recovery(
        self, decision_id: ControllerDecisionId | str, *, inspection_outcome: str, now: str | None = None
    ) -> ControllerGenerationStatus:
        """Compatibility wrapper for authenticated explicit recovery requests."""

        identity = ControllerDecisionId(str(decision_id))
        try:
            claim = self.reserve_controller_recovery_inspection(identity, now=now)
        except StaleWriter:
            # A caller may have reserved the read in an earlier process turn;
            # completion remains one-shot and is still CAS-protected.
            raise
        return self.complete_controller_recovery_inspection(
            identity, inspection_outcome=inspection_outcome, claim=claim, now=now
        )

    def wake_outbox(self, *, state: str | None = None) -> tuple[JsonObject, ...]:
        if state is not None and state not in {
            "not_applicable",
            "pending",
            "starting",
            "delivered",
            "failed",
            "ambiguous",
            "suppressed",
        }:
            raise ValueError("wake state is invalid")
        query = "SELECT * FROM wake_outbox"
        parameters: tuple[object, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY created_at, delivery_id"
        return tuple(self._queue_row(row) for row in self._db().execute(query, parameters).fetchall())

    def claim_wake(self, delivery_id: str) -> JsonObject | None:
        now = utc_now()
        with self._transaction():
            row = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            if row is None:
                raise RecordNotFound(f"wake delivery does not exist: {delivery_id}")
            decision = (
                self._db()
                .execute("SELECT state FROM controller_decisions WHERE decision_id = ?", (row["decision_id"],))
                .fetchone()
            )
            if decision is None:
                raise CorruptSchemaError("wake delivery has no controller decision")
            if row["state"] == "suppressed" or decision["state"] != ControllerDecisionState.PENDING_DELIVERY.value:
                if row["state"] in {"not_applicable", "delivered", "suppressed"}:
                    return self._queue_row(row)
                raise StaleWriter("wake delivery is no longer pending controller delivery")
            if row["state"] == "not_applicable" or row["state"] == "delivered":
                return self._queue_row(row)
            if row["state"] == "ambiguous":
                raise StaleWriter("ambiguous wake delivery requires reconciliation")
            if int(row["attempt_count"]) >= 2:
                self._db().execute(
                    "UPDATE wake_outbox SET state = 'failed', updated_at = ? WHERE delivery_id = ?",
                    (now, delivery_id),
                )
            else:
                self._db().execute(
                    "UPDATE wake_outbox SET state = 'starting', attempt_count = attempt_count + 1, updated_at = ? WHERE delivery_id = ? AND state IN ('pending','failed')",
                    (now, delivery_id),
                )
            updated = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._queue_row(updated)

    def reconcile_inflight_wakes(self) -> int:
        """Conservatively close source wakes left in the SDK call window.

        A harness crash cannot prove whether the SDK created a source turn.
        Marking the row ambiguous is therefore the only safe restart action;
        callers with authoritative shared-session evidence may then reconcile
        it to one recorded source turn through ``reconcile_wake_delivery``.
        """

        with self._transaction():
            now = utc_now()
            cursor = self._db().execute(
                "UPDATE wake_outbox SET state = 'ambiguous', updated_at = ? WHERE state = 'starting'",
                (now,),
            )
            return int(cursor.rowcount)

    def reconcile_exhausted_wake_notifications(self) -> int:
        """Release legacy notification-gated decisions to the model controller.

        Source-task wake delivery is a best-effort UI hint, not controller
        lifecycle authority. Older rows closed the decision when both wake
        attempts failed. Reopen only that exact pre-claim/pre-generation
        shape; ambiguous identity windows and every other human-attention
        reason remain fail-closed.
        """

        with self._transaction():
            now = utc_now()
            deadline = self._expires_after(now, _CONTROLLER_DECISION_WINDOW_SECONDS)
            cursor = self._db().execute(
                "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, "
                "deadline = CASE WHEN deadline < ? THEN ? ELSE deadline END, "
                "human_attention_reason = NULL, updated_at = ? "
                "WHERE state = 'human_attention_required' AND action_id IS NULL "
                "AND claimant_kind IS NULL AND claimant_id IS NULL AND claim_token_sha256 IS NULL "
                "AND human_attention_reason = 'source wake delivery budget exhausted' "
                "AND EXISTS (SELECT 1 FROM wake_outbox w WHERE w.decision_id = controller_decisions.decision_id "
                "AND w.state = 'failed' AND w.attempt_count = 2) "
                "AND EXISTS (SELECT 1 FROM controller_decision_generations g "
                "WHERE g.decision_id = controller_decisions.decision_id "
                "AND g.generation = controller_decisions.current_generation AND g.state = 'prepared' "
                "AND g.controller_thread_id IS NULL AND g.controller_turn_id IS NULL)",
                (deadline, deadline, now),
            )
            return int(cursor.rowcount)

    def reconcile_wake_delivery(self, delivery_id: str, *, source_turn_id: str) -> JsonObject:
        """Bind one authoritative source-turn identity to an ambiguous wake."""

        if (
            not isinstance(source_turn_id, str)
            or not source_turn_id
            or len(source_turn_id) > 512
            or any(character.isspace() for character in source_turn_id)
        ):
            raise ValueError("source wake turn identity is invalid")
        with self._transaction():
            row = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            if row is None:
                raise RecordNotFound(f"wake delivery does not exist: {delivery_id}")
            if row["state"] == "delivered":
                if row["source_turn_id"] != source_turn_id:
                    raise StaleWriter("wake delivery identity is immutable")
                return self._queue_row(row)
            if row["state"] != "ambiguous":
                raise StaleWriter("wake delivery is not awaiting reconciliation")
            self._db().execute(
                "UPDATE wake_outbox SET state = 'delivered', source_turn_id = ?, updated_at = ? "
                "WHERE delivery_id = ? AND state = 'ambiguous'",
                (source_turn_id, utc_now(), delivery_id),
            )
            self._db().execute(
                "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, updated_at = ? "
                "WHERE decision_id = ? AND state = 'pending_delivery'",
                (utc_now(), row["decision_id"]),
            )
            updated = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._queue_row(updated)

    def record_wake_delivery(
        self,
        delivery_id: str,
        *,
        outcome: str,
        source_turn_id: str | None = None,
    ) -> JsonObject:
        if outcome not in {"delivered", "failed", "ambiguous"}:
            raise ValueError("wake delivery outcome is invalid")
        now = utc_now()
        with self._transaction():
            row = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            if row is None:
                raise RecordNotFound(f"wake delivery does not exist: {delivery_id}")
            if row["state"] == "delivered":
                if outcome != "delivered" or source_turn_id != row["source_turn_id"]:
                    raise StaleWriter("wake delivery identity is immutable")
                return self._queue_row(row)
            if row["state"] == "ambiguous":
                if outcome == "ambiguous" and source_turn_id is None:
                    return self._queue_row(row)
                raise StaleWriter("ambiguous wake delivery requires reconciliation")
            if row["state"] != "starting":
                raise StaleWriter("wake delivery is not in its claimed SDK call state")
            if outcome == "delivered":
                if (
                    not isinstance(source_turn_id, str)
                    or not source_turn_id
                    or len(source_turn_id) > 512
                    or any(character.isspace() for character in source_turn_id)
                    or row["source_thread_id"] is None
                ):
                    raise StaleWriter("wake delivery requires a source turn identity")
                self._db().execute(
                    "UPDATE wake_outbox SET state = 'delivered', source_turn_id = ?, updated_at = ? WHERE delivery_id = ? AND state = 'starting'",
                    (source_turn_id, now, delivery_id),
                )
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, updated_at = ? "
                    "WHERE decision_id = ? AND state = 'pending_delivery'",
                    (now, row["decision_id"]),
                )
            elif outcome == "ambiguous":
                if source_turn_id is not None:
                    raise StaleWriter("ambiguous wake delivery cannot carry a source turn identity")
                self._db().execute(
                    "UPDATE wake_outbox SET state = 'ambiguous', updated_at = ? WHERE delivery_id = ? AND state = 'starting'",
                    (now, delivery_id),
                )
                self._db().execute(
                    "UPDATE controller_decisions SET state = 'human_attention_required', human_attention_reason = ?, "
                    "claim_lease_expires_at = NULL, updated_at = ? WHERE decision_id = ? AND state = 'pending_delivery'",
                    ("source wake delivery is ambiguous", now, row["decision_id"]),
                )
            else:
                if source_turn_id is not None:
                    raise StaleWriter("failed wake delivery cannot carry a source turn identity")
                if int(row["attempt_count"]) >= 2:
                    next_state = "failed"
                else:
                    next_state = "pending"
                self._db().execute(
                    "UPDATE wake_outbox SET state = ?, updated_at = ? WHERE delivery_id = ? AND state = 'starting'",
                    (next_state, now, delivery_id),
                )
                if next_state == "failed":
                    deadline = self._expires_after(now, _CONTROLLER_DECISION_WINDOW_SECONDS)
                    self._db().execute(
                        "UPDATE controller_decisions SET state = 'awaiting_claim', revision = revision + 1, "
                        "deadline = CASE WHEN deadline < ? THEN ? ELSE deadline END, "
                        "human_attention_reason = NULL, claim_lease_expires_at = NULL, updated_at = ? "
                        "WHERE decision_id = ? AND state = 'pending_delivery'",
                        (deadline, deadline, now, row["decision_id"]),
                    )
            updated = self._db().execute("SELECT * FROM wake_outbox WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._queue_row(updated)

    def harness_authority(self) -> JsonObject | None:
        row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
        return self._queue_row(row) if row is not None else None

    def harness_refresh_fenced(self) -> bool:
        """Return whether the durable harness row rejects new work."""

        row = self._db().execute("SELECT requested_shutdown FROM harness_authority WHERE singleton = 1").fetchone()
        return row is not None and int(row["requested_shutdown"]) == 1

    def _raise_if_active_harness_children_in_transaction(self) -> None:
        # ``Popen`` cannot share the SQLite transaction that claims a queue
        # item.  Treat a durable pre-launch claim as active handoff work so a
        # refresh cannot win the gap between queue claim and worker-liveness
        # binding.  The harness will reconcile stale claims on restart.
        queue = (
            self._db()
            .execute("SELECT 1 FROM dispatch_queue WHERE state IN ('claimed', 'starting', 'running') LIMIT 1")
            .fetchone()
        )
        if queue is not None:
            raise HarnessRefreshBlocked("harness refresh deferred while a worker launch is active")
        worker = self._db().execute("SELECT 1 FROM worker_liveness WHERE exited_at IS NULL LIMIT 1").fetchone()
        if worker is not None:
            raise HarnessRefreshBlocked("harness refresh deferred while a worker child is active")
        controller = (
            self._db()
            .execute(
                "SELECT 1 FROM controller_decision_generations WHERE state IN ('delivery_starting', 'active') LIMIT 1"
            )
            .fetchone()
        )
        if controller is not None:
            raise HarnessRefreshBlocked("harness refresh deferred while a controller child is active")

    def assert_harness_refresh_allowed(self) -> None:
        """Fail closed if a child is active before a refresh fence is armed."""

        with self._transaction():
            self._raise_if_active_harness_children_in_transaction()

    def arm_harness_refresh_fence(self) -> JsonObject | None:
        """Atomically reject active children and arm the existing shutdown fact."""

        with self._transaction():
            self._raise_if_active_harness_children_in_transaction()
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            if row is None:
                return None
            self._db().execute("UPDATE harness_authority SET requested_shutdown = 1 WHERE singleton = 1")
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            return self._queue_row(row)

    def arm_predecessor_refresh_fence(self) -> JsonObject | None:
        """Fence a v18 supervisor before an explicit v18 -> v19 migration.

        This method is available only on an ``allow_legacy`` opener.  It is
        intentionally narrower than the normal harness fence: no current
        schema validation or migration is performed, and the caller must
        stop the fenced predecessor before opening a migrating Ledger.
        """

        if self._legacy_schema_version != SchemaVersion(18):
            raise MigrationRequired("predecessor refresh fencing requires a legacy schema-v18 opener")
        with self._transaction(validate_authority=False):
            self._raise_if_active_harness_children_in_transaction()
            row = self._db().execute("SELECT * FROM supervisor_authority WHERE singleton = 1").fetchone()
            if row is None:
                return None
            if int(row["requested_shutdown"]) not in {0, 1}:
                raise CorruptSchemaError("supervisor authority shutdown fence is malformed")
            self._db().execute("UPDATE supervisor_authority SET requested_shutdown = 1 WHERE singleton = 1")
            row = self._db().execute("SELECT * FROM supervisor_authority WHERE singleton = 1").fetchone()
            return self._queue_row(row)

    def predecessor_refresh_fenced(self) -> bool:
        """Return the v18 predecessor fence without opening the v19 authority."""

        if self._legacy_schema_version != SchemaVersion(18):
            raise MigrationRequired("predecessor refresh fencing requires a legacy schema-v18 opener")
        row = self._db().execute("SELECT requested_shutdown FROM supervisor_authority WHERE singleton = 1").fetchone()
        return row is not None and int(row["requested_shutdown"]) == 1

    def acquire_harness(
        self,
        *,
        repository_root: Path | str,
        state_root: Path | str,
        pid: int,
        process_birth_identity: str,
        executable_digest: str,
        version: str,
        owner_nonce_sha256: str,
        lease_seconds: float = 30.0,
    ) -> JsonObject:
        if pid <= 0 or lease_seconds <= 0 or not process_birth_identity or not version:
            raise ValueError("harness lease facts are invalid")
        digests = (executable_digest, owner_nonce_sha256)
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in digests):
            raise ValueError("harness digest is invalid")
        now = utc_now()
        # ISO timestamps are only compared lexically in this module; callers
        # provide a bounded lease and stale ownership is conservatively denied.
        expires = datetime.fromisoformat(now.removesuffix("Z")).replace(tzinfo=timezone.utc).timestamp() + lease_seconds
        expires_at = (
            datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        with self._transaction():
            existing = self.harness_authority()
            if existing is not None:
                handoff_fenced = int(existing["requested_shutdown"]) == 1
                if (
                    not handoff_fenced
                    and str(existing["expires_at"]) > now
                    and str(existing["owner_nonce_sha256"]) != owner_nonce_sha256
                ):
                    raise StaleWriter("another live harness owns the repository")
                epoch = int(existing["epoch"]) + 1
                self._db().execute("DELETE FROM harness_authority WHERE singleton = 1")
            else:
                epoch = 1
            self._db().execute(
                "INSERT INTO harness_authority(singleton, repository_root, state_root, epoch, owner_nonce_sha256, pid, process_birth_identity, acquired_at, renewed_at, expires_at, executable_digest, version, requested_shutdown) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    os.fspath(Path(repository_root).resolve()),
                    os.fspath(Path(state_root).resolve()),
                    epoch,
                    owner_nonce_sha256,
                    pid,
                    process_birth_identity,
                    now,
                    now,
                    expires_at,
                    executable_digest,
                    version,
                ),
            )
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            return self._queue_row(row)

    def renew_harness(self, *, epoch: int, owner_nonce_sha256: str, lease_seconds: float = 30.0) -> JsonObject:
        if epoch <= 0 or not re.fullmatch(r"[0-9a-f]{64}", owner_nonce_sha256):
            raise ValueError("harness renewal facts are invalid")
        now = utc_now()
        expires = datetime.fromisoformat(now.removesuffix("Z")).replace(tzinfo=timezone.utc).timestamp() + lease_seconds
        expires_at = (
            datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        with self._transaction():
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            if (
                row is None
                or int(row["epoch"]) != epoch
                or not secrets.compare_digest(str(row["owner_nonce_sha256"]), owner_nonce_sha256)
            ):
                raise StaleWriter("harness lease is stale")
            self._db().execute(
                "UPDATE harness_authority SET renewed_at = ?, expires_at = ? WHERE singleton = 1", (now, expires_at)
            )
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            return self._queue_row(row)

    def request_harness_shutdown(self, *, epoch: int, owner_nonce_sha256: str) -> JsonObject:
        with self._transaction():
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            if (
                row is None
                or int(row["epoch"]) != epoch
                or not secrets.compare_digest(str(row["owner_nonce_sha256"]), owner_nonce_sha256)
            ):
                raise StaleWriter("harness lease is stale")
            self._db().execute("UPDATE harness_authority SET requested_shutdown = 1 WHERE singleton = 1")
            row = self._db().execute("SELECT * FROM harness_authority WHERE singleton = 1").fetchone()
            return self._queue_row(row)

    def transition(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        to_state: WorkflowState | str,
        *,
        expected_state: WorkflowState | str,
        reason: WorkflowReason | ReasonCode | str | BaseException | None = None,
        event_type: str | None = None,
        data: Mapping[str, object] | None = None,
        dispatch_id: DispatchId | str | None = None,
    ) -> EventRecord:
        """Advance a milestone and append its causal event atomically.

        The run and milestone are always explicit; milestone ids may be reused
        safely across independent runs.
        """

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        target = coerce_state(to_state)
        expected_value = coerce_state(expected_state)
        normalized_reason = _reason(reason)
        if data is not None:
            _empty_object(data, field_name="event data")
        dispatch = DispatchId(dispatch_id) if dispatch_id is not None else None
        normalized_event_type = _event_type(event_type or "state_transition")
        if normalized_event_type != "state_transition" or dispatch is not None:
            raise ValueError("normal state transitions cannot carry dispatch authority")
        if normalized_reason is not None and normalized_reason.code is ReasonCode.DISPATCH_CLAIMED:
            raise ValueError("dispatch_claimed reason is reserved for claim_dispatch")
        with self._transaction():
            current = self.get_milestone(run, milestone)
            if current.state is not expected_value:
                raise StaleWriter(
                    f"stale milestone writer: expected {expected_value.value}, current is {current.state.value}"
                )
            if current.state is WorkflowState.PLANNED and target is WorkflowState.STARTING:
                raise InvalidTransition("PLANNED -> STARTING is reserved for claim_dispatch")
            if not is_transition_allowed(current.state, target):
                raise InvalidTransition(f"{current.state.value} -> {target.value} is not allowed")
            now = utc_now()
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(current.run_id), str(current.milestone_id)),
            )
            self._fault("after_state_update")
            if normalized_reason is None and target in TERMINAL_STATES:
                normalized_reason = WorkflowReason(ReasonCode.TERMINAL_OUTCOME)
            event = self._append_event_in_transaction(
                current.run_id,
                current.milestone_id,
                from_state=current.state,
                to_state=target,
                event_type=normalized_event_type,
                reason=normalized_reason,
                dispatch_id=dispatch,
                data=None,
            )
            self._fault("after_event_insert")
            expected_milestone = MilestoneRecord(
                current.run_id,
                current.milestone_id,
                target,
                current.created_at,
                now,
                current.metadata,
            )
            self._verify_milestone(expected_milestone)
            return self._verify_event(event)

    def _append_event_in_transaction(
        self,
        run_id: RunId,
        milestone_id: MilestoneId,
        *,
        from_state: WorkflowState | None,
        to_state: WorkflowState,
        event_type: str,
        reason: WorkflowReason | None,
        dispatch_id: DispatchId | None,
        data: JsonObject | None,
    ) -> EventRecord:
        last = (
            self._db()
            .execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ? AND milestone_id = ?",
                (str(run_id), str(milestone_id)),
            )
            .fetchone()
        )
        sequence = EventSequence(int(last[0]) + 1)
        event_id = EventId(f"{run_id}/{milestone_id}/{int(sequence)}")
        encoded_data = None
        occurred_at = utc_now()
        if data is not None:
            _empty_object(data, field_name="event data")
        self._db().execute(
            "INSERT INTO events(event_id, run_id, milestone_id, sequence, from_state, to_state, event_type, "
            "reason_code, reason_detail, dispatch_id, data_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event_id),
                str(run_id),
                str(milestone_id),
                int(sequence),
                from_state.value if from_state is not None else None,
                to_state.value,
                event_type,
                reason.code.value if reason is not None else None,
                reason.detail if reason is not None else None,
                str(dispatch_id) if dispatch_id is not None else None,
                encoded_data,
                occurred_at,
            ),
        )
        expected = EventRecord(
            event_id,
            run_id,
            milestone_id,
            sequence,
            from_state,
            to_state,
            event_type,
            reason,
            occurred_at,
            dispatch_id,
            data,
        )
        return self._verify_event(expected)

    # ------------------------------------------------------------------
    # H4 typed review/repair/recovery facts
    # ------------------------------------------------------------------

    def record_review_transition(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        to_state: WorkflowState | str,
        *,
        expected_state: WorkflowState | str,
        phase: LifecyclePhase | str,
        kind: str,
        data: Mapping[str, object],
        reason: WorkflowReason | ReasonCode | str | BaseException | None = None,
    ) -> EventRecord:
        """Persist one review fact and its state transition atomically.

        H2 callers continue to reject arbitrary event payloads.  Review callers
        use this explicit envelope so review, repair, budget, and recovery
        facts remain causally ordered in the existing SQLite ledger.
        """

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        target = coerce_state(to_state)
        expected_value = coerce_state(expected_state)
        try:
            phase_value = phase if isinstance(phase, LifecyclePhase) else LifecyclePhase(phase)
        except ValueError as exc:
            raise ValueError(f"unknown review lifecycle phase: {phase!r}") from exc
        if not isinstance(kind, str) or not kind or len(kind) > 128 or any(character.isspace() for character in kind):
            raise ValueError("review lifecycle kind must be a bounded whitespace-free string")
        if not isinstance(data, Mapping):
            raise TypeError("review lifecycle data must be a mapping")
        payload = {
            "schema": "codex-flow/h4/v1",
            "phase": phase_value.value,
            "kind": kind,
            "payload": dict(data),
        }
        normalized_reason = _reason(reason)
        if normalized_reason is None and target in TERMINAL_STATES:
            normalized_reason = WorkflowReason(ReasonCode.TERMINAL_OUTCOME)
        with self._transaction():
            current = self.get_milestone(run, milestone)
            if current.state is not expected_value:
                raise StaleWriter(
                    f"stale review writer: expected {expected_value.value}, current is {current.state.value}"
                )
            if not is_transition_allowed(current.state, target):
                raise InvalidTransition(f"{current.state.value} -> {target.value} is not allowed")
            now = utc_now()
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(run), str(milestone)),
            )
            self._fault("after_state_update")
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=current.state,
                to_state=target,
                event_type="state_transition",
                reason=normalized_reason,
                dispatch_id=None,
                data=None,
            )
            _envelope, encoded = _encode_h4_event_data(payload)
            lifecycle_sequence = (
                int(
                    self._db()
                    .execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM h4.lifecycle WHERE run_id = ? AND milestone_id = ?",
                        (str(run), str(milestone)),
                    )
                    .fetchone()[0]
                )
                + 1
            )
            self._db().execute(
                "INSERT INTO h4.lifecycle(run_id, milestone_id, sequence, phase, kind, data_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(run), str(milestone), lifecycle_sequence, phase_value.value, kind, encoded, now),
            )
            self._fault("after_event_insert")
            return self._verify_event(event)

    def review_lifecycle(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[LifecycleRecord, ...]:
        """Read review facts from the authoritative event stream in sequence order."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = (
            self._db()
            .execute(
                "SELECT sequence, phase, kind, data_json FROM h4.lifecycle "
                "WHERE run_id = ? AND milestone_id = ? ORDER BY sequence",
                (str(run), str(milestone)),
            )
            .fetchall()
        )
        records: list[LifecycleRecord] = []
        for row in rows:
            envelope = _decode_event_data(row["data_json"])
            if envelope is None:
                raise SchemaError("review event envelope is missing")
            body = envelope.get("__h4")
            if not isinstance(body, dict) or body.get("schema") != "codex-flow/h4/v1":
                raise SchemaError("review event envelope is invalid")
            phase = body.get("phase")
            kind = body.get("kind")
            payload = body.get("payload")
            if not isinstance(phase, str) or not isinstance(kind, str) or not isinstance(payload, dict):
                raise SchemaError("review event envelope has invalid typed fields")
            if str(row["phase"]) != phase or str(row["kind"]) != kind:
                raise SchemaError("H4 lifecycle index disagrees with its typed envelope")
            records.append(LifecycleRecord(LifecyclePhase(phase), kind, int(row["sequence"]), payload))
        return tuple(records)

    def record_review_fact(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        phase: LifecyclePhase | str,
        kind: str,
        data: Mapping[str, object],
    ) -> LifecycleRecord:
        """Persist a typed review fact that does not itself change workflow state."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        phase_value = phase if isinstance(phase, LifecyclePhase) else LifecyclePhase(phase)
        if not isinstance(kind, str) or not kind or len(kind) > 128 or any(character.isspace() for character in kind):
            raise ValueError("review lifecycle kind must be a bounded whitespace-free string")
        payload = {
            "schema": "codex-flow/h4/v1",
            "phase": phase_value.value,
            "kind": kind,
            "payload": dict(data),
        }
        envelope, encoded = _encode_h4_event_data(payload)
        with self._transaction():
            self.get_milestone(run, milestone)
            row = (
                self._db()
                .execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM h4.lifecycle WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            sequence = int(row[0]) + 1
            occurred_at = utc_now()
            self._db().execute(
                "INSERT INTO h4.lifecycle(run_id, milestone_id, sequence, phase, kind, data_json, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(run), str(milestone), sequence, phase_value.value, kind, encoded, occurred_at),
            )
        body = envelope["__h4"]
        if not isinstance(body, dict) or not isinstance(body.get("payload"), dict):
            raise CorruptSchemaError("review fact envelope has an invalid payload")
        return LifecycleRecord(phase_value, kind, sequence, body["payload"])

    def record_review_recovery(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        decision: RecoveryDecision,
    ) -> LifecycleRecord:
        return self.record_review_fact(
            run_id,
            milestone_id,
            phase=LifecyclePhase.RECOVERY,
            kind="recovery_decision",
            data={
                "outcome": decision.outcome.value,
                "rationale": decision.rationale,
                "checkpoint": decision.checkpoint,
                "finding_ids": list(decision.finding_ids),
                "external_prerequisite": decision.external_prerequisite,
                "decision_request_id": decision.decision_request_id,
            },
        )

    def record_review_decision_request(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        request: DecisionRequest,
    ) -> LifecycleRecord:
        return self.record_review_fact(
            run_id,
            milestone_id,
            phase=LifecyclePhase.DECISION,
            kind="decision_request",
            data={
                "request_id": request.request_id,
                "question": request.question,
                "options": list(request.options),
                "blocking": request.blocking,
            },
        )

    def record_review_decision_response(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        response: DecisionResponse,
    ) -> LifecycleRecord:
        requests = [
            record
            for record in self.review_lifecycle(run_id, milestone_id)
            if record.kind == "decision_request" and record.data.get("request_id") == response.request_id
        ]
        if not requests:
            raise RecordNotFound(f"decision request {response.request_id} does not exist")
        options = requests[-1].data.get("options")
        if not isinstance(options, list | tuple) or response.choice not in options:
            raise ValueError("decision response choice is not one of the persisted request options")
        return self.record_review_fact(
            run_id,
            milestone_id,
            phase=LifecyclePhase.DECISION,
            kind="decision_response",
            data={
                "request_id": response.request_id,
                "choice": response.choice,
                "rationale": response.rationale,
            },
        )

    def record_review_budget(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        budget: Budget,
        exhaustion: BudgetExhaustion | None = None,
    ) -> LifecycleRecord:
        return self.record_review_fact(
            run_id,
            milestone_id,
            phase=LifecyclePhase.BUDGET,
            kind="budget_snapshot",
            data={
                "max_turns": budget.max_turns,
                "max_repairs": budget.max_repairs,
                "max_reviews": budget.max_reviews,
                "max_compactions": budget.max_compactions,
                "max_validations": budget.max_validations,
                "turns": budget.turns,
                "repairs": budget.repairs,
                "reviews": budget.reviews,
                "compactions": budget.compactions,
                "validations": budget.validations,
                "exhaustion": exhaustion.value if exhaustion is not None else None,
            },
        )

    # ------------------------------------------------------------------
    # H3 controller facts
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_terminal_execution(record: ExecutionRecord) -> None:
        if record.status in _TERMINAL_EXECUTION_STATUSES:
            raise StaleWriter("terminal execution rejects further mutation")

    def get_app_native_dispatch(self, dispatch_id: DispatchId | str) -> AppNativeDispatchRecord:
        dispatch = DispatchId(dispatch_id)
        row = (
            self._db().execute("SELECT * FROM app_native_dispatches WHERE dispatch_id = ?", (str(dispatch),)).fetchone()
        )
        if row is None:
            raise RecordNotFound(f"App-native dispatch {dispatch} does not exist")
        return self._app_native_from_row(row)

    def get_app_native_for_execution(
        self, run_id: RunId | str, milestone_id: MilestoneId | str
    ) -> AppNativeDispatchRecord | None:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute(
                "SELECT * FROM app_native_dispatches WHERE run_id = ? AND milestone_id = ?",
                (str(run), str(milestone)),
            )
            .fetchone()
        )
        return self._app_native_from_row(row) if row is not None else None

    @staticmethod
    def _same_native_action(left: AppNativeTaskAction, right: AppNativeTaskAction) -> bool:
        return (
            left.schema_version,
            left.dispatch_id,
            left.model,
            left.reasoning_effort,
            left.workspace_path,
            left.prompt,
            left.output_schema,
            left.action,
            left.non_blocking,
            left.result_contract_sha256,
        ) == (
            right.schema_version,
            right.dispatch_id,
            right.model,
            right.reasoning_effort,
            right.workspace_path,
            right.prompt,
            right.output_schema,
            right.action,
            right.non_blocking,
            right.result_contract_sha256,
        )

    def prepare_app_native_dispatch(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        action: AppNativeTaskAction,
        *,
        workspace_baseline_head_sha: str,
        workspace_baseline: tuple[tuple[str, str], ...],
        git_authority_before_sha256: str,
        controller_state_sha256: str,
    ) -> AppNativeDispatchRecord:
        """Atomically claim the executor and retain exactly one host action."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        expected_dispatch = DispatchId.from_parts(run, milestone, "executor", 1)
        if action.dispatch_id != expected_dispatch:
            raise ValueError("App-native action must address executor generation one")
        baseline_head = _git_sha(workspace_baseline_head_sha, field_name="App-native baseline HEAD")
        baseline_json, baseline_sha256 = _encode_workspace_baseline(workspace_baseline)
        git_before = _sha256(git_authority_before_sha256, field_name="App-native Git authority")
        controller_state = _sha256(controller_state_sha256, field_name="controller-state digest")
        action_json = _encode_json(action.to_json())
        token_sha256 = claim_token_sha256(action.claim_token)
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(run, milestone)
            if execution.workspace_path != action.workspace_path:
                raise StaleWriter("App-native action workspace does not match its durable execution")
            lease_row = (
                self._db()
                .execute("SELECT * FROM workspace_leases WHERE workspace_path = ?", (str(action.workspace_path),))
                .fetchone()
            )
            if lease_row is None:
                raise StaleWriter("App-native prepare requires its durable workspace lease")
            lease = self._workspace_lease_from_row(lease_row)
            if lease.owner_run_id != run:
                raise StaleWriter("App-native action workspace does not match its durable lease")
            existing_row = (
                self._db()
                .execute(
                    "SELECT * FROM app_native_dispatches WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if existing_row is not None:
                existing = self._app_native_from_row(existing_row)
                if (
                    not self._same_native_action(existing.action, action)
                    or existing.workspace_baseline_head_sha != baseline_head
                    or existing.workspace_baseline != workspace_baseline
                    or existing.git_authority_before_sha256 != git_before
                    or existing.controller_state_sha256 != controller_state
                ):
                    raise StaleWriter("App-native execution is already prepared with different facts")
                return existing
            self._reject_terminal_execution(execution)
            if execution.status is not ExecutionStatus.PLANNED or execution.checkpoint not in {
                ControllerCheckpoint.WORKSPACE_LEASED,
                ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE,
            }:
                raise StaleWriter("App-native prepare requires one planned leased execution")
            current = self.get_milestone(run, milestone)
            if current.state is not WorkflowState.PLANNED:
                raise InvalidTransition(f"App-native prepare requires PLANNED, found {current.state.value}")
            owner = (
                self._db()
                .execute(
                    "SELECT dispatch_id FROM dispatches WHERE run_id = ? AND milestone_id = ? AND role = 'executor'",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if owner is not None:
                raise DispatchConflict(f"App-native executor is already owned by {owner['dispatch_id']}")
            self._db().execute(
                "INSERT INTO dispatches(dispatch_id, run_id, milestone_id, role, generation, claimed_at) "
                "VALUES (?, ?, ?, 'executor', 1, ?)",
                (str(expected_dispatch), str(run), str(milestone), now),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.STARTING.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.PLANNED,
                to_state=WorkflowState.STARTING,
                event_type="dispatch_claimed",
                reason=WorkflowReason(ReasonCode.DISPATCH_CLAIMED),
                dispatch_id=expected_dispatch,
                data=None,
            )
            self._db().execute(
                "INSERT INTO app_native_dispatches(dispatch_id, run_id, milestone_id, state, action_json, "
                "action_sha256, claim_token_sha256, host_id, thread_id, result_json, result_sha256, "
                "workspace_baseline_head_sha, workspace_baseline_json, workspace_baseline_sha256, "
                "git_authority_before_sha256, git_authority_after_sha256, controller_state_sha256, "
                "workspace_terminal_head_sha, "
                "workspace_terminal_json, workspace_terminal_sha256, prepared_at, bound_at, completed_at) "
                "VALUES (?, ?, ?, 'prepared', ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, NULL, ?, "
                "NULL, NULL, NULL, ?, NULL, NULL)",
                (
                    str(expected_dispatch),
                    str(run),
                    str(milestone),
                    action_json,
                    action.sha256,
                    token_sha256,
                    baseline_head,
                    baseline_json,
                    baseline_sha256,
                    git_before,
                    controller_state,
                    now,
                ),
            )
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._verify_event(event)
            return self.get_app_native_dispatch(expected_dispatch)

    def bind_app_native_dispatch(
        self,
        dispatch_id: DispatchId | str,
        *,
        claim_token: str,
        receipt: HostReceipt,
    ) -> AppNativeDispatchRecord:
        dispatch = DispatchId(dispatch_id)
        token_sha256 = claim_token_sha256(claim_token)
        identity = receipt.identity
        now = utc_now()
        with self._transaction():
            current = self.get_app_native_dispatch(dispatch)
            claim = self.get_dispatch(dispatch)
            if claim_token_sha256(current.action.claim_token) != token_sha256:
                raise StaleWriter("App-native bind capability does not match its prepared action")
            try:
                receipt.verify(current.action, claim_token=claim_token)
            except ValueError as exc:
                raise StaleWriter(str(exc)) from exc
            if current.state is AppNativeState.CANCELLED:
                raise StaleWriter("cancelled App-native dispatch rejects late bind receipt replay")
            if current.identity is not None:
                if current.identity != identity:
                    raise StaleWriter("App-native dispatch is already bound to a different host identity")
                return current
            if current.state is not AppNativeState.PREPARED:
                raise StaleWriter("only a prepared App-native dispatch can be bound")
            execution = self.get_execution(claim.run_id, claim.milestone_id)
            if execution.status is not ExecutionStatus.PLANNED:
                raise StaleWriter("App-native bind requires a planned execution")
            workflow = self.get_milestone(claim.run_id, claim.milestone_id)
            if workflow.state is not WorkflowState.STARTING:
                raise StaleWriter("App-native bind requires a STARTING workflow state")
            self._db().execute(
                "UPDATE app_native_dispatches SET state = 'bound', host_id = ?, thread_id = ?, bound_at = ? "
                "WHERE dispatch_id = ?",
                (identity.host_id, identity.thread_id.id, now, str(dispatch)),
            )
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, thread_id = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ExecutionStatus.THREAD_STARTED.value,
                    ControllerCheckpoint.THREAD_IDENTITY_DURABLE.value,
                    identity.thread_id.id,
                    now,
                    str(claim.run_id),
                    str(claim.milestone_id),
                ),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.RUNNING.value, now, str(claim.run_id), str(claim.milestone_id)),
            )
            event = self._append_event_in_transaction(
                claim.run_id,
                claim.milestone_id,
                from_state=WorkflowState.STARTING,
                to_state=WorkflowState.RUNNING,
                event_type="state_transition",
                reason=None,
                dispatch_id=None,
                data=None,
            )
            self._verify_event(event)
            return self.get_app_native_dispatch(dispatch)

    def record_app_native_result(
        self,
        dispatch_id: DispatchId | str,
        *,
        claim_token: str,
        identity: HostIdentity,
        result: ModelFacingResult,
        validation: ValidationObservation,
        protected_after_sha256: str,
        git_authority_after_sha256: str,
        workspace_terminal_head_sha: str | None,
        workspace_terminal: tuple[tuple[str, str], ...] | None,
    ) -> AppNativeDispatchRecord:
        """Persist one exact terminal host result and controller gate atomically."""

        dispatch = DispatchId(dispatch_id)
        token_sha256 = claim_token_sha256(claim_token)
        protected_after = _sha256(protected_after_sha256, field_name="protected-path digest")
        git_after = _sha256(git_authority_after_sha256, field_name="App-native Git authority")
        result_json = _encode_json(result.to_json())
        result_sha256 = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        if (workspace_terminal_head_sha is None) != (workspace_terminal is None):
            raise ValueError("App-native terminal workspace authority must be complete or absent")
        terminal_head = (
            _git_sha(workspace_terminal_head_sha, field_name="App-native terminal HEAD")
            if workspace_terminal_head_sha is not None
            else None
        )
        terminal_json: str | None = None
        terminal_sha256: str | None = None
        if workspace_terminal is not None:
            terminal_json, terminal_sha256 = _encode_workspace_baseline(workspace_terminal)
        passed = (
            validation.exit_code == 0
            and not validation.timed_out
            and result.status is ModelResultStatus.COMPLETED
            and all(item.passed for item in result.validations)
        )
        if passed and (terminal_head is None or terminal_json is None or terminal_sha256 is None):
            raise StaleWriter("completed App-native execution requires terminal workspace authority")
        app_state = AppNativeState.COMPLETED if passed else AppNativeState.FAILED
        execution_status = ExecutionStatus.COMPLETED if passed else ExecutionStatus.FAILED
        workflow_state = WorkflowState.COMPLETED if passed else WorkflowState.FAILED
        now = utc_now()
        with self._transaction():
            current = self.get_app_native_dispatch(dispatch)
            claim = self.get_dispatch(dispatch)
            if claim_token_sha256(current.action.claim_token) != token_sha256:
                raise StaleWriter("App-native result capability does not match its prepared action")
            if current.identity != identity:
                raise StaleWriter("App-native result identity does not match the exact bound host thread")
            if current.result is not None:
                terminal_execution = self.get_execution(claim.run_id, claim.milestone_id)
                if (
                    current.result != result
                    or current.git_authority_after_sha256 != git_after
                    or current.workspace_terminal_head_sha != terminal_head
                    or current.workspace_terminal != workspace_terminal
                    or current.state is not app_state
                    or terminal_execution.validation != validation
                    or terminal_execution.protected_after_sha256 != protected_after
                ):
                    raise StaleWriter("App-native dispatch already owns different terminal facts")
                return current
            if current.state is not AppNativeState.BOUND:
                raise StaleWriter("App-native result requires a bound dispatch")
            execution = self.get_execution(claim.run_id, claim.milestone_id)
            if execution.status is not ExecutionStatus.THREAD_STARTED or execution.thread_id != identity.thread_id:
                raise StaleWriter("App-native result requires its durable bound execution identity")
            workflow = self.get_milestone(claim.run_id, claim.milestone_id)
            if workflow.state is not WorkflowState.RUNNING:
                raise StaleWriter("App-native result requires a RUNNING workflow state")
            self._db().execute(
                "UPDATE app_native_dispatches SET state = ?, result_json = ?, result_sha256 = ?, "
                "git_authority_after_sha256 = ?, workspace_terminal_head_sha = ?, workspace_terminal_json = ?, "
                "workspace_terminal_sha256 = ?, completed_at = ? WHERE dispatch_id = ?",
                (
                    app_state.value,
                    result_json,
                    result_sha256,
                    git_after,
                    terminal_head if passed else None,
                    terminal_json if passed else None,
                    terminal_sha256 if passed else None,
                    now,
                    str(dispatch),
                ),
            )
            validation_argv: object = (
                list(validation.argv)
                if validation.error_code is None
                else {"argv": list(validation.argv), "error_code": validation.error_code.value}
            )
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, result_json = ?, validation_argv_json = ?, "
                "validation_exit_code = ?, validation_stdout_sha256 = ?, validation_stderr_sha256 = ?, "
                "validation_timed_out = ?, validation_duration_seconds = ?, protected_after_sha256 = ?, "
                "updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    execution_status.value,
                    ControllerCheckpoint.RESULT_DURABLE.value,
                    result_json,
                    _encode_json(validation_argv),
                    validation.exit_code,
                    validation.stdout_sha256,
                    validation.stderr_sha256,
                    int(validation.timed_out),
                    validation.duration_seconds,
                    protected_after,
                    now,
                    str(claim.run_id),
                    str(claim.milestone_id),
                ),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (workflow_state.value, now, str(claim.run_id), str(claim.milestone_id)),
            )
            event = self._append_event_in_transaction(
                claim.run_id,
                claim.milestone_id,
                from_state=WorkflowState.RUNNING,
                to_state=workflow_state,
                event_type="state_transition",
                reason=WorkflowReason(ReasonCode.TERMINAL_OUTCOME if passed else ReasonCode.EXECUTION_FAILURE),
                dispatch_id=None,
                data=None,
            )
            self._verify_event(event)
            return self.get_app_native_dispatch(dispatch)

    def record_native_profile(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        native_profile_sha256: str,
        native_compatibility_sha256: str,
        effective_permission: NativePermissionAuthority,
        workspace_baseline_head_sha: str,
        workspace_baseline: tuple[tuple[str, str], ...],
        git_authority_before_sha256: str,
    ) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        profile = _sha256(native_profile_sha256, field_name="native profile digest")
        compatibility = _sha256(native_compatibility_sha256, field_name="native compatibility digest")
        permission_json = _encode_json(effective_permission.facts)
        permission_sha256 = hashlib.sha256(permission_json.encode("utf-8")).hexdigest()
        baseline_head = _git_sha(workspace_baseline_head_sha, field_name="workspace baseline HEAD")
        baseline_json, baseline_sha256 = _encode_workspace_baseline(workspace_baseline)
        git_authority = _sha256(git_authority_before_sha256, field_name="Git authority digest")
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(run, milestone)
            self._reject_terminal_execution(execution)
            if execution.status is not ExecutionStatus.PLANNED or execution.checkpoint not in {
                ControllerCheckpoint.WORKSPACE_LEASED,
                ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE,
            }:
                raise StaleWriter("native profile binding requires a planned leased execution")
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM execution_integrity WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if existing is not None:
                record = self._execution_integrity_from_row(existing)
                if (
                    record.native_profile_sha256 != profile
                    or record.native_compatibility_sha256 != compatibility
                    or record.effective_permission != effective_permission
                    or record.workspace_baseline_head_sha != baseline_head
                    or record.workspace_baseline != workspace_baseline
                    or record.git_authority_before_sha256 != git_authority
                ):
                    raise StaleWriter("execution is already bound to a different native profile")
                return record
            self._db().execute(
                "INSERT INTO execution_integrity(run_id, milestone_id, provenance, native_profile_sha256, "
                "native_compatibility_sha256, effective_permission_json, effective_permission_sha256, "
                "workspace_baseline_head_sha, workspace_baseline_json, workspace_baseline_sha256, "
                "workspace_terminal_head_sha, workspace_terminal_json, workspace_terminal_sha256, "
                "turn_started_at, git_authority_before_sha256, git_authority_after_sha256, created_at, updated_at) "
                "VALUES (?, ?, 'controller_v5', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)",
                (
                    str(run),
                    str(milestone),
                    profile,
                    compatibility,
                    permission_json,
                    permission_sha256,
                    baseline_head,
                    baseline_json,
                    baseline_sha256,
                    git_authority,
                    now,
                    now,
                ),
            )
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            return self.get_execution_integrity(run, milestone)

    def rebind_native_profile_for_resume(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        native_profile_sha256: str,
        native_compatibility_sha256: str,
        candidate_permission: NativePermissionAuthority,
    ) -> ExecutionIntegrityRecord:
        """Atomically retain the meet of durable and current native authority."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        profile = _sha256(native_profile_sha256, field_name="native profile digest")
        compatibility = _sha256(native_compatibility_sha256, field_name="native compatibility digest")
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(run, milestone)
            self._reject_terminal_execution(execution)
            if execution.status is not ExecutionStatus.THREAD_STARTED or execution.thread_id is None:
                raise StaleWriter("native profile resume rebind requires a durable active SDK thread")
            current = self.get_execution_integrity(run, milestone)
            if current.provenance not in {"controller_v4", "controller_v5"} or current.effective_permission is None:
                raise NativeCompatibilityConflict("execution lacks resumable native compatibility authority")
            if current.native_compatibility_sha256 != compatibility:
                raise NativeCompatibilityConflict("native provider/routing/discovery compatibility changed")
            try:
                effective = current.effective_permission.meet(candidate_permission)
            except ValueError as exc:
                raise NativePermissionConflict("native permission authority changed incompatibly") from exc
            permission_json = _encode_json(effective.facts)
            permission_sha256 = hashlib.sha256(permission_json.encode("utf-8")).hexdigest()
            self._db().execute(
                "UPDATE execution_integrity SET native_profile_sha256 = ?, effective_permission_json = ?, "
                "effective_permission_sha256 = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (profile, permission_json, permission_sha256, now, str(run), str(milestone)),
            )
            return self.get_execution_integrity(run, milestone)

    def record_git_authority_before(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        git_authority_before_sha256: str,
    ) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        authority = _sha256(git_authority_before_sha256, field_name="Git authority digest")
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(run, milestone)
            self._reject_terminal_execution(execution)
            if execution.status is not ExecutionStatus.THREAD_STARTED or execution.thread_id is None:
                raise StaleWriter("Git authority binding requires a durable active SDK thread")
            current = self.get_execution_integrity(run, milestone)
            if current.git_authority_before_sha256 is not None:
                if current.git_authority_before_sha256 != authority:
                    raise StaleWriter("execution Git authority changed before its SDK turn")
                return current
            self._db().execute(
                "UPDATE execution_integrity SET git_authority_before_sha256 = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (authority, now, str(run), str(milestone)),
            )
            return self.get_execution_integrity(run, milestone)

    def get_execution_integrity(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionIntegrityRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute(
                "SELECT * FROM execution_integrity WHERE run_id = ? AND milestone_id = ?",
                (str(run), str(milestone)),
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(f"unknown execution integrity: {run}/{milestone}")
        return self._execution_integrity_from_row(row)

    def plan_execution(
        self,
        capsule: ExecutionCapsule,
        *,
        capsule_path: Path,
        capsule_sha256: str,
        protected_before_sha256: str,
    ) -> ExecutionRecord:
        capsule_digest = _sha256(capsule_sha256, field_name="capsule digest")
        protected_digest = _sha256(protected_before_sha256, field_name="protected-path digest")
        if not capsule_path.is_absolute():
            raise ValueError("capsule path must be absolute")
        now = utc_now()
        with self._transaction():
            self.get_run(capsule.run_id)
            self.get_milestone(capsule.run_id, capsule.milestone_id)
            existing = (
                self._db()
                .execute(
                    "SELECT * FROM executions WHERE run_id = ? AND milestone_id = ?",
                    (str(capsule.run_id), str(capsule.milestone_id)),
                )
                .fetchone()
            )
            if existing is not None:
                record = self._execution_from_row(existing)
                self._reject_terminal_execution(record)
                expected = (
                    capsule_path,
                    capsule_digest,
                    capsule.workspace_path,
                    capsule.model,
                    capsule.reasoning_effort,
                    protected_digest,
                )
                actual = (
                    record.capsule_path,
                    record.capsule_sha256,
                    record.workspace_path,
                    record.model,
                    record.reasoning_effort,
                    record.protected_before_sha256,
                )
                if actual != expected:
                    raise WorkspaceLeaseConflict("execution capsule conflicts with its durable plan")
                return record
            self._db().execute(
                "INSERT INTO executions(run_id, milestone_id, capsule_path, capsule_sha256, workspace_path, "
                "model, reasoning_effort, status, checkpoint, thread_id, turn_id, turn_output_json, result_json, "
                "validation_argv_json, validation_exit_code, validation_stdout_sha256, "
                "validation_stderr_sha256, validation_timed_out, validation_duration_seconds, "
                "protected_before_sha256, protected_after_sha256, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?)",
                (
                    str(capsule.run_id),
                    str(capsule.milestone_id),
                    str(capsule_path),
                    capsule_digest,
                    str(capsule.workspace_path),
                    capsule.model,
                    capsule.reasoning_effort.value,
                    ExecutionStatus.PLANNED.value,
                    ControllerCheckpoint.CAPSULE_PLANNED.value,
                    protected_digest,
                    now,
                    now,
                ),
            )
            return self.get_execution(capsule.run_id, capsule.milestone_id)

    def acquire_workspace_lease(
        self,
        capsule: ExecutionCapsule,
    ) -> WorkspaceLeaseRecord:
        now = utc_now()
        with self._transaction():
            execution = self.get_execution(capsule.run_id, capsule.milestone_id)
            self._reject_terminal_execution(execution)
            if execution.workspace_path != capsule.workspace_path:
                raise WorkspaceLeaseConflict("durable execution selected a different workspace")
            owner = (
                self._db()
                .execute(
                    "SELECT * FROM workspace_leases WHERE owner_run_id = ? AND lane = ?",
                    (str(capsule.run_id), capsule.lane),
                )
                .fetchone()
            )
            path_owner = (
                self._db()
                .execute(
                    "SELECT * FROM workspace_leases WHERE workspace_path = ?",
                    (str(capsule.workspace_path),),
                )
                .fetchone()
            )
            if owner is not None or path_owner is not None:
                if (
                    owner is not None
                    and path_owner is not None
                    and owner["workspace_path"] != path_owner["workspace_path"]
                ):
                    raise WorkspaceLeaseConflict("run lane and workspace are leased by different owners")
                candidate = owner or path_owner
                lease = self._workspace_lease_from_row(candidate)
                expected = (
                    capsule.workspace_path,
                    capsule.repository_root,
                    capsule.workspace_mode,
                    capsule.branch,
                    capsule.base_sha,
                    capsule.lane,
                    capsule.run_id,
                )
                actual = (
                    lease.workspace_path,
                    lease.repository_root,
                    lease.mode,
                    lease.branch,
                    lease.base_sha,
                    lease.lane,
                    lease.owner_run_id,
                )
                if actual != expected:
                    raise WorkspaceLeaseConflict("workspace is already leased under different facts")
                active_statuses = tuple(
                    status.value
                    for status in ExecutionStatus
                    if status
                    not in {
                        ExecutionStatus.COMPLETED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    }
                )
                placeholders = ", ".join("?" for _ in active_statuses)
                active_owner = (
                    self._db()
                    .execute(
                        "SELECT run_id, milestone_id, status FROM executions "
                        f"WHERE workspace_path = ? AND status IN ({placeholders}) "
                        "AND checkpoint != ? AND NOT (run_id = ? AND milestone_id = ?) "
                        "ORDER BY created_at, run_id, milestone_id LIMIT 1",
                        (
                            str(capsule.workspace_path),
                            *active_statuses,
                            ControllerCheckpoint.CAPSULE_PLANNED.value,
                            str(capsule.run_id),
                            str(capsule.milestone_id),
                        ),
                    )
                    .fetchone()
                )
                if active_owner is not None:
                    raise WorkspaceLeaseConflict(
                        "workspace already has a nonterminal execution owner "
                        f"{active_owner['run_id']}/{active_owner['milestone_id']}"
                    )
            else:
                self._db().execute(
                    "INSERT INTO workspace_leases(workspace_path, repository_root, mode, branch, base_sha, lane, "
                    "owner_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(capsule.workspace_path),
                        str(capsule.repository_root),
                        capsule.workspace_mode.value,
                        capsule.branch,
                        capsule.base_sha,
                        capsule.lane,
                        str(capsule.run_id),
                        now,
                    ),
                )
                lease = self._workspace_lease_from_row(
                    self._db()
                    .execute("SELECT * FROM workspace_leases WHERE workspace_path = ?", (str(capsule.workspace_path),))
                    .fetchone()
                )
            if (
                execution.status is ExecutionStatus.PLANNED
                and execution.checkpoint is ControllerCheckpoint.CAPSULE_PLANNED
            ):
                self._db().execute(
                    "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                    (
                        ControllerCheckpoint.WORKSPACE_LEASED.value,
                        now,
                        str(capsule.run_id),
                        str(capsule.milestone_id),
                    ),
                )
            return lease

    def record_thread_identity(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        thread_id: ThreadIdentity,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if current.thread_id is not None:
                if current.thread_id != thread_id:
                    raise StaleWriter("execution already owns a different SDK thread identity")
                return current
            if (
                current.status is not ExecutionStatus.PLANNED
                or current.checkpoint is not ControllerCheckpoint.THREAD_STARTING
            ):
                raise StaleWriter("thread identity requires a planned leased execution")
            workflow = self.get_milestone(run, milestone)
            if workflow.state is not WorkflowState.STARTING:
                raise StaleWriter("thread identity requires a STARTING workflow state")
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, thread_id = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ExecutionStatus.THREAD_STARTED.value,
                    ControllerCheckpoint.THREAD_IDENTITY_DURABLE.value,
                    thread_id.id,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.RUNNING.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.STARTING,
                to_state=WorkflowState.RUNNING,
                event_type="state_transition",
                reason=None,
                dispatch_id=None,
                data=None,
            )
            expected = self.get_execution(run, milestone)
            if self.current_state(run, milestone) is not WorkflowState.RUNNING:
                raise CorruptSchemaError("thread identity write did not advance workflow state")
            self._verify_event(event)
            return expected

    def record_thread_starting(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if current.status is not ExecutionStatus.PLANNED:
                raise StaleWriter("SDK thread start requires a planned execution")
            if current.checkpoint is ControllerCheckpoint.THREAD_STARTING:
                return current
            if current.checkpoint is not ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE:
                raise StaleWriter("SDK thread start requires a durable per-milestone workspace baseline")
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.THREAD_STARTING.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            return self.get_execution(run, milestone)

    def record_pre_identity_uncertainty(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if (
                current.status is not ExecutionStatus.PLANNED
                or current.checkpoint is not ControllerCheckpoint.THREAD_STARTING
                or current.thread_id is not None
            ):
                raise StaleWriter("pre-identity uncertainty requires the active thread-start boundary")
            self._db().execute(
                "UPDATE executions SET status = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (ExecutionStatus.UNCERTAIN_PRE_IDENTITY.value, now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def _reject_thread_start_authorization(
        self, run_id: RunId | str, milestone_id: MilestoneId | str
    ) -> ExecutionRecord:
        """Restore the last safe checkpoint when pre-call authorization rejects."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if (
                current.status is not ExecutionStatus.PLANNED
                or current.checkpoint is not ControllerCheckpoint.THREAD_STARTING
                or current.thread_id is not None
            ):
                raise StaleWriter("thread-start authorization rejection requires the uncalled external boundary")
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (ControllerCheckpoint.WORKSPACE_BASELINE_DURABLE.value, now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def record_turn_starting(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        """Durably mark the SDK turn boundary before making an external call."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if current.status is not ExecutionStatus.THREAD_STARTED or current.thread_id is None:
                raise StaleWriter("turn start requires a durable SDK thread")
            if current.turn_id is not None:
                return current
            if current.turn_output == _TURN_STARTING_MARKER:
                return current
            integrity = self.get_execution_integrity(run, milestone)
            if integrity.workspace_baseline is None:
                raise StaleWriter("turn start requires a durable per-milestone workspace baseline")
            self._db().execute(
                "UPDATE executions SET turn_output_json = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (_encode_json(_TURN_STARTING_MARKER), now, str(run), str(milestone)),
            )
            self._db().execute(
                "UPDATE execution_integrity SET turn_started_at = COALESCE(turn_started_at, ?), updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (now, now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def _reject_turn_authorization(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        """Clear the causal marker only when authorization proves no turn was called."""

        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            integrity = self.get_execution_integrity(run, milestone)
            if (
                current.status is not ExecutionStatus.THREAD_STARTED
                or current.thread_id is None
                or current.turn_id is not None
                or current.turn_output != _TURN_STARTING_MARKER
                or integrity.turn_started_at is None
            ):
                raise StaleWriter("turn authorization rejection requires the uncalled external boundary")
            self._db().execute(
                "UPDATE executions SET turn_output_json = NULL, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (now, str(run), str(milestone)),
            )
            self._db().execute(
                "UPDATE execution_integrity SET turn_started_at = NULL, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (now, str(run), str(milestone)),
            )
            return self.get_execution(run, milestone)

    def record_turn(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        observation: TurnObservation,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if current.status is not ExecutionStatus.THREAD_STARTED or current.thread_id is None:
                raise StaleWriter("turn observation requires a durable active SDK thread")
            if current.thread_id != observation.thread_id:
                raise StaleWriter("turn observation belongs to a different SDK thread")
            integrity = self.get_execution_integrity(run, milestone)
            if current.turn_id is not None:
                if current.turn_id != observation.turn_id:
                    raise StaleWriter("execution already owns a different SDK turn")
                if integrity.turn_started_at is None:
                    raise CorruptSchemaError("durable SDK turn lacks causal turn-start authority")
                durable_events = tuple(
                    LifecycleEvent(int(row["sequence"]), str(row["method"]), row["event_turn_id"])
                    for row in self._db()
                    .execute(
                        "SELECT sequence, method, event_turn_id FROM sdk_lifecycle_events "
                        "WHERE run_id = ? AND milestone_id = ? AND turn_id = ? ORDER BY sequence",
                        (str(run), str(milestone), current.turn_id),
                    )
                    .fetchall()
                )
                if current.turn_output != (observation.structured_output or {}) or durable_events != observation.events:
                    raise StaleWriter("execution already owns different durable SDK turn facts")
                return current
            if integrity.turn_started_at is None or current.turn_output != _TURN_STARTING_MARKER:
                raise StaleWriter("turn observation requires prior durable turn-start authority")
            for event in observation.events:
                self._db().execute(
                    "INSERT INTO sdk_lifecycle_events(run_id, milestone_id, turn_id, sequence, method, event_turn_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(run),
                        str(milestone),
                        observation.turn_id,
                        event.sequence,
                        event.method,
                        event.turn_id,
                    ),
                )
            self._db().execute(
                "UPDATE executions SET checkpoint = ?, turn_id = ?, turn_output_json = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ControllerCheckpoint.TURN_DURABLE.value,
                    observation.turn_id,
                    _encode_json(observation.structured_output or {}),
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            return self.get_execution(run, milestone)

    def record_terminal_execution(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        *,
        result: JsonObject,
        validation: ValidationObservation,
        protected_after_sha256: str,
        git_authority_after_sha256: str,
        workspace_terminal_head_sha: str | None,
        workspace_terminal: tuple[tuple[str, str], ...] | None,
    ) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        after_digest = _sha256(protected_after_sha256, field_name="protected-path digest")
        git_after = _sha256(git_authority_after_sha256, field_name="Git authority digest")
        if (workspace_terminal_head_sha is None) != (workspace_terminal is None):
            raise ValueError("terminal workspace authority must be complete or absent")
        terminal_head = (
            _git_sha(workspace_terminal_head_sha, field_name="workspace terminal HEAD")
            if workspace_terminal_head_sha is not None
            else None
        )
        terminal_json: str | None = None
        terminal_sha256: str | None = None
        if workspace_terminal is not None:
            terminal_json, terminal_sha256 = _encode_workspace_baseline(workspace_terminal)
        now = utc_now()
        outcome_status = result.get("status") if isinstance(result, Mapping) else None
        outcome_failed = isinstance(outcome_status, str) and outcome_status.strip().lower() in {
            "failed",
            "failure",
            "blocked",
            "cancelled",
            "error",
        }
        terminal = (
            ExecutionStatus.COMPLETED
            if validation.exit_code == 0 and not validation.timed_out and not outcome_failed
            else ExecutionStatus.FAILED
        )
        if terminal is ExecutionStatus.COMPLETED and (
            terminal_head is None or terminal_json is None or terminal_sha256 is None
        ):
            raise StaleWriter("completed execution requires accepted terminal workspace authority")
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            if current.status is not ExecutionStatus.THREAD_STARTED or current.turn_id is None:
                raise StaleWriter("terminal result requires a durable SDK turn")
            integrity = self.get_execution_integrity(run, milestone)
            if integrity.git_authority_before_sha256 is None:
                raise StaleWriter("terminal result requires a durable pre-turn Git authority snapshot")
            workflow = self.get_milestone(run, milestone)
            if workflow.state is not WorkflowState.RUNNING:
                raise StaleWriter("terminal result requires a RUNNING workflow state")
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, result_json = ?, validation_argv_json = ?, "
                "validation_exit_code = ?, validation_stdout_sha256 = ?, validation_stderr_sha256 = ?, "
                "validation_timed_out = ?, validation_duration_seconds = ?, protected_after_sha256 = ?, "
                "updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (
                    terminal.value,
                    ControllerCheckpoint.RESULT_DURABLE.value,
                    _encode_json(result),
                    _encode_json(
                        list(validation.argv)
                        if validation.error_code is None
                        else {
                            "argv": list(validation.argv),
                            "error_code": validation.error_code.value,
                        }
                    ),
                    validation.exit_code,
                    validation.stdout_sha256,
                    validation.stderr_sha256,
                    int(validation.timed_out),
                    validation.duration_seconds,
                    after_digest,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            self._db().execute(
                "UPDATE execution_integrity SET git_authority_after_sha256 = ?, "
                "workspace_terminal_head_sha = ?, workspace_terminal_json = ?, workspace_terminal_sha256 = ?, "
                "updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    git_after,
                    terminal_head if terminal is ExecutionStatus.COMPLETED else None,
                    terminal_json if terminal is ExecutionStatus.COMPLETED else None,
                    terminal_sha256 if terminal is ExecutionStatus.COMPLETED else None,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            target = WorkflowState.COMPLETED if terminal is ExecutionStatus.COMPLETED else WorkflowState.FAILED
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (target.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=WorkflowState.RUNNING,
                to_state=target,
                event_type="state_transition",
                reason=WorkflowReason(
                    ReasonCode.TERMINAL_OUTCOME
                    if terminal is ExecutionStatus.COMPLETED
                    else ReasonCode.EXECUTION_FAILURE
                ),
                dispatch_id=None,
                data=None,
            )
            result_record = self.get_execution(run, milestone)
            if self.current_state(run, milestone) is not target:
                raise CorruptSchemaError("terminal execution write did not advance workflow state")
            self._verify_event(event)
            return result_record

    def cancel_execution(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        now = utc_now()
        with self._transaction():
            current = self.get_execution(run, milestone)
            self._reject_terminal_execution(current)
            workflow = self.get_milestone(run, milestone)
            if workflow.state in TERMINAL_STATES:
                return current
            queue = (
                self._db()
                .execute(
                    "SELECT * FROM dispatch_queue WHERE run_id = ? AND milestone_id = ?",
                    (str(run), str(milestone)),
                )
                .fetchone()
            )
            if queue is not None:
                self._cancel_queue_row_in_transaction(queue)
            self._db().execute(
                "UPDATE executions SET status = ?, checkpoint = ?, updated_at = ? "
                "WHERE run_id = ? AND milestone_id = ?",
                (
                    ExecutionStatus.CANCELLED.value,
                    ControllerCheckpoint.RESULT_DURABLE.value,
                    now,
                    str(run),
                    str(milestone),
                ),
            )
            app_native = self.get_app_native_for_execution(run, milestone)
            if app_native is not None:
                self._db().execute(
                    "UPDATE app_native_dispatches SET state = 'cancelled', completed_at = ? "
                    "WHERE run_id = ? AND milestone_id = ?",
                    (now, str(run), str(milestone)),
                )
            self._db().execute(
                "UPDATE milestones SET current_state = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
                (WorkflowState.CANCELLED.value, now, str(run), str(milestone)),
            )
            event = self._append_event_in_transaction(
                run,
                milestone,
                from_state=workflow.state,
                to_state=WorkflowState.CANCELLED,
                event_type="state_transition",
                reason=None,
                dispatch_id=None,
                data=None,
            )
            result_record = self.get_execution(run, milestone)
            self._verify_event(event)
            return result_record

    def get_execution(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> ExecutionRecord:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        row = (
            self._db()
            .execute("SELECT * FROM executions WHERE run_id = ? AND milestone_id = ?", (str(run), str(milestone)))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"execution {run}/{milestone} does not exist")
        return self._execution_from_row(row)

    def get_workspace_lease(self, workspace_path: Path) -> WorkspaceLeaseRecord:
        canonical_workspace = _canonical_workspace_path(workspace_path)
        row = (
            self._db()
            .execute("SELECT * FROM workspace_leases WHERE workspace_path = ?", (str(canonical_workspace),))
            .fetchone()
        )
        if row is None:
            raise RecordNotFound(f"workspace lease {canonical_workspace} does not exist")
        return self._workspace_lease_from_row(row)

    def completed_workspace_predecessors(
        self,
        run_id: RunId | str,
        milestone_id: MilestoneId | str,
        workspace_path: Path,
    ) -> tuple[ExecutionRecord, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        canonical_workspace = _canonical_workspace_path(workspace_path)
        rows = (
            self._db()
            .execute(
                "SELECT * FROM executions WHERE run_id = ? AND workspace_path = ? AND milestone_id != ? "
                "AND status = ? ORDER BY created_at, milestone_id",
                (str(run), str(canonical_workspace), str(milestone), ExecutionStatus.COMPLETED.value),
            )
            .fetchall()
        )
        return tuple(self._execution_from_row(row) for row in rows)

    def sdk_lifecycle_events(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[LifecycleEvent, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = (
            self._db()
            .execute(
                "SELECT * FROM sdk_lifecycle_events WHERE run_id = ? AND milestone_id = ? ORDER BY turn_id, sequence",
                (str(run), str(milestone)),
            )
            .fetchall()
        )
        return tuple(LifecycleEvent(int(row["sequence"]), str(row["method"]), row["event_turn_id"]) for row in rows)

    # ------------------------------------------------------------------
    # Read-only snapshots and recovery facts
    # ------------------------------------------------------------------

    def events(self, run_id: RunId | str, milestone_id: MilestoneId | str) -> tuple[EventRecord, ...]:
        run = _run_id(run_id)
        milestone = _milestone_id(milestone_id)
        rows = (
            self._db()
            .execute(
                "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? ORDER BY sequence",
                (str(run), str(milestone)),
            )
            .fetchall()
        )
        return tuple(self._event_from_row(row) for row in rows)

    def recovery_facts(self, run_id: RunId | str) -> tuple[RecoveryFact, ...]:
        run = _run_id(run_id)
        with self._read_transaction():
            rows = (
                self._db()
                .execute(
                    "SELECT d.*, m.current_state FROM dispatches d JOIN milestones m "
                    "ON m.run_id = d.run_id AND m.milestone_id = d.milestone_id "
                    "WHERE d.run_id = ? ORDER BY d.milestone_id, d.role, d.generation",
                    (str(run),),
                )
                .fetchall()
            )
            self._fault("after_recovery_dispatch_rows_read")
            facts: list[RecoveryFact] = []
            for row in rows:
                state = _row_state(row["current_state"])
                if state in TERMINAL_STATES:
                    continue
                dispatch = self._dispatch_from_row(row)
                last_sequence = (
                    self._db()
                    .execute(
                        "SELECT MAX(sequence) FROM events WHERE run_id = ? AND milestone_id = ?",
                        (str(dispatch.run_id), str(dispatch.milestone_id)),
                    )
                    .fetchone()[0]
                )
                last = (
                    self._db()
                    .execute(
                        "SELECT * FROM events WHERE run_id = ? AND milestone_id = ? AND sequence = ?",
                        (str(dispatch.run_id), str(dispatch.milestone_id), int(last_sequence)),
                    )
                    .fetchone()
                )
                if last is None:
                    raise SchemaError(f"dispatch {dispatch.dispatch_id} has no causal event")
                facts.append(RecoveryFact(dispatch, state, self._event_from_row(last)))
            return tuple(facts)

    def snapshot(self, run_id: RunId | str) -> LedgerSnapshot:
        run = _run_id(run_id)
        # A read transaction gives the artifact projector one committed view
        # without allowing it to become an authority for future writes.
        with self._read_transaction():
            record = self.get_run(run)
            milestone_rows = (
                self._db()
                .execute("SELECT * FROM milestones WHERE run_id = ? ORDER BY milestone_id", (str(run),))
                .fetchall()
            )
            dispatch_rows = (
                self._db()
                .execute(
                    "SELECT * FROM dispatches WHERE run_id = ? ORDER BY milestone_id, role, generation", (str(run),)
                )
                .fetchall()
            )
            event_rows = (
                self._db()
                .execute("SELECT * FROM events WHERE run_id = ? ORDER BY milestone_id, sequence", (str(run),))
                .fetchall()
            )
            return LedgerSnapshot(
                record,
                tuple(self._milestone_from_row(row) for row in milestone_rows),
                tuple(self._dispatch_from_row(row) for row in dispatch_rows),
                tuple(self._event_from_row(row) for row in event_rows),
            )

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        self._db().execute("BEGIN")
        try:
            yield
        finally:
            try:
                self._db().execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            RunId(row["run_id"]),
            str(row["created_at"]),
            str(row["closed_at"]) if row["closed_at"] is not None else None,
            _decode_object(row["metadata_json"], field_name="run metadata"),
        )

    @staticmethod
    def _milestone_from_row(row: sqlite3.Row) -> MilestoneRecord:
        return MilestoneRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            _row_state(row["current_state"]),
            str(row["created_at"]),
            str(row["updated_at"]),
            _decode_object(row["metadata_json"], field_name="milestone metadata"),
        )

    @staticmethod
    def _dispatch_from_row(row: sqlite3.Row) -> DispatchClaim:
        return DispatchClaim(
            DispatchId(row["dispatch_id"]),
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            RoleId(row["role"]),
            Generation(row["generation"]),
            str(row["claimed_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EventRecord:
        reason_code = row["reason_code"]
        reason = None
        if reason_code is not None:
            try:
                if row["reason_detail"] is not None:
                    raise SchemaError("persisted reason detail is not allowed")
                code = ReasonCode(reason_code)
                reason_type: type[WorkflowReason] = {
                    ReasonCode.TRANSPORT_FAILURE: PreIdentityTransportFailure,
                    ReasonCode.EXECUTION_FAILURE: PostIdentityExecutionFailure,
                    ReasonCode.REVIEW_REJECTED: ReviewRejected,
                    ReasonCode.TERMINAL_OUTCOME: TerminalOutcome,
                }.get(code, WorkflowReason)
                if reason_type is WorkflowReason:
                    reason = WorkflowReason(code)
                else:
                    reason = reason_type()
            except ValueError as exc:
                raise SchemaError(f"unknown persisted reason code: {reason_code!r}") from exc
        data = _decode_object(row["data_json"], field_name="event data") if row["data_json"] is not None else None
        return EventRecord(
            EventId(row["event_id"]),
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            EventSequence(row["sequence"]),
            _row_state(row["from_state"]) if row["from_state"] is not None else None,
            _row_state(row["to_state"]),
            str(row["event_type"]),
            reason,
            str(row["occurred_at"]),
            DispatchId(row["dispatch_id"]) if row["dispatch_id"] is not None else None,
            data,
        )

    @staticmethod
    def _workspace_lease_from_row(row: sqlite3.Row) -> WorkspaceLeaseRecord:
        return WorkspaceLeaseRecord(
            _persisted_canonical_path(row["workspace_path"], label="workspace lease path"),
            _persisted_canonical_path(row["repository_root"], label="repository root"),
            WorkspaceMode(str(row["mode"])),
            str(row["branch"]),
            str(row["base_sha"]),
            str(row["lane"]),
            RunId(row["owner_run_id"]),
            str(row["created_at"]),
        )

    @staticmethod
    def _app_native_from_row(row: sqlite3.Row) -> AppNativeDispatchRecord:
        action_value = _decode_json_object(str(row["action_json"]), field_name="App-native action")
        if action_value is None:  # pragma: no cover - non-null row contract
            raise CorruptSchemaError("App-native action is missing")
        action = AppNativeTaskAction.from_json(action_value)
        if str(action.dispatch_id) != str(row["dispatch_id"]) or action.sha256 != str(row["action_sha256"]):
            raise CorruptSchemaError("App-native action digest or dispatch identity does not match")
        if claim_token_sha256(action.claim_token) != str(row["claim_token_sha256"]):
            raise CorruptSchemaError("App-native claim capability digest does not match")
        identity = None
        if row["host_id"] is not None and row["thread_id"] is not None:
            identity = HostIdentity(str(row["host_id"]), ThreadIdentity(str(row["thread_id"])))
        result = None
        if row["result_json"] is not None:
            result_value = _decode_json_object(str(row["result_json"]), field_name="App-native result")
            if result_value is None:  # pragma: no cover - non-null row contract
                raise CorruptSchemaError("App-native result is missing")
            result = ModelFacingResult.from_json(result_value)
            result_digest = hashlib.sha256(_encode_json(result.to_json()).encode("utf-8")).hexdigest()
            if result_digest != str(row["result_sha256"]):
                raise CorruptSchemaError("App-native result digest does not match")
        baseline = _decode_workspace_baseline(
            str(row["workspace_baseline_json"]), str(row["workspace_baseline_sha256"])
        )
        terminal_workspace = None
        if row["workspace_terminal_json"] is not None:
            terminal_workspace = _decode_workspace_baseline(
                str(row["workspace_terminal_json"]), str(row["workspace_terminal_sha256"])
            )
        return AppNativeDispatchRecord(
            action,
            AppNativeState(str(row["state"])),
            identity,
            result,
            _git_sha(str(row["workspace_baseline_head_sha"]), field_name="App-native baseline HEAD"),
            baseline,
            _sha256(str(row["git_authority_before_sha256"]), field_name="App-native Git authority"),
            (
                _sha256(str(row["git_authority_after_sha256"]), field_name="App-native Git authority")
                if row["git_authority_after_sha256"] is not None
                else None
            ),
            _sha256(str(row["controller_state_sha256"]), field_name="controller-state digest"),
            (
                _git_sha(str(row["workspace_terminal_head_sha"]), field_name="App-native terminal HEAD")
                if row["workspace_terminal_head_sha"] is not None
                else None
            ),
            terminal_workspace,
            str(row["prepared_at"]),
            str(row["bound_at"]) if row["bound_at"] is not None else None,
            str(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ExecutionRecord:
        validation: ValidationObservation | None = None
        argv_raw = row["validation_argv_json"]
        if argv_raw is not None:
            try:
                argv_decoded = strict_json_loads(str(argv_raw))
            except ValueError as exc:
                raise SchemaError("validation argv is not valid JSON") from exc
            error_code: ValidationFailureCode | None = None
            if isinstance(argv_decoded, dict):
                encoded_argv = argv_decoded.get("argv")
                encoded_error = argv_decoded.get("error_code")
                if set(argv_decoded) != {"argv", "error_code"} or not isinstance(encoded_argv, list):
                    raise SchemaError("validation observation envelope is invalid")
                if encoded_error is not None:
                    try:
                        error_code = ValidationFailureCode(str(encoded_error))
                    except ValueError as exc:
                        raise SchemaError("validation error code is unsupported") from exc
                argv_decoded = encoded_argv
            if not isinstance(argv_decoded, list) or any(not isinstance(item, str) for item in argv_decoded):
                raise SchemaError("validation argv is not a string list")
            required = (
                row["validation_exit_code"],
                row["validation_stdout_sha256"],
                row["validation_stderr_sha256"],
                row["validation_timed_out"],
                row["validation_duration_seconds"],
            )
            if any(value is None for value in required):
                raise SchemaError("validation observation is incomplete")
            validation = ValidationObservation(
                tuple(argv_decoded),
                int(row["validation_exit_code"]),
                _sha256(str(row["validation_stdout_sha256"]), field_name="validation stdout digest"),
                _sha256(str(row["validation_stderr_sha256"]), field_name="validation stderr digest"),
                bool(row["validation_timed_out"]),
                float(row["validation_duration_seconds"]),
                error_code,
            )
        return ExecutionRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            _persisted_canonical_path(row["workspace_path"], label="execution workspace path"),
            _persisted_canonical_path(row["capsule_path"], label="capsule path"),
            _sha256(str(row["capsule_sha256"]), field_name="capsule digest"),
            str(row["model"]),
            ReasoningEffort(str(row["reasoning_effort"])),
            ExecutionStatus(str(row["status"])),
            ControllerCheckpoint(str(row["checkpoint"])),
            ThreadIdentity(str(row["thread_id"])) if row["thread_id"] is not None else None,
            str(row["turn_id"]) if row["turn_id"] is not None else None,
            _decode_json_object(row["turn_output_json"], field_name="turn output"),
            _decode_json_object(row["result_json"], field_name="execution result"),
            validation,
            _sha256(str(row["protected_before_sha256"]), field_name="protected before digest"),
            (
                _sha256(str(row["protected_after_sha256"]), field_name="protected after digest")
                if row["protected_after_sha256"] is not None
                else None
            ),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _execution_integrity_from_row(row: sqlite3.Row) -> ExecutionIntegrityRecord:
        native_profile = row["native_profile_sha256"] if "native_profile_sha256" in row.keys() else None
        native_compatibility = (
            row["native_compatibility_sha256"] if "native_compatibility_sha256" in row.keys() else None
        )
        permission_raw = row["effective_permission_json"] if "effective_permission_json" in row.keys() else None
        permission_digest = row["effective_permission_sha256"] if "effective_permission_sha256" in row.keys() else None
        effective_permission = None
        if permission_raw is not None:
            decoded = _decode_json_object(str(permission_raw), field_name="effective native permission")
            if decoded is None:  # pragma: no cover - non-null row contract
                raise CorruptSchemaError("effective native permission is missing")
            effective_permission = NativePermissionAuthority.from_facts(decoded)
            actual_digest = hashlib.sha256(_encode_json(effective_permission.facts).encode("utf-8")).hexdigest()
            if permission_digest != actual_digest:
                raise CorruptSchemaError("effective native permission digest does not match its facts")
        baseline_head = row["workspace_baseline_head_sha"] if "workspace_baseline_head_sha" in row.keys() else None
        baseline_raw = row["workspace_baseline_json"] if "workspace_baseline_json" in row.keys() else None
        baseline_digest = row["workspace_baseline_sha256"] if "workspace_baseline_sha256" in row.keys() else None
        baseline = None
        if any(value is not None for value in (baseline_head, baseline_raw, baseline_digest)):
            if any(value is None for value in (baseline_head, baseline_raw, baseline_digest)):
                raise CorruptSchemaError("workspace baseline fact is incomplete")
            baseline = _decode_workspace_baseline(str(baseline_raw), str(baseline_digest))
        terminal_head = row["workspace_terminal_head_sha"] if "workspace_terminal_head_sha" in row.keys() else None
        terminal_raw = row["workspace_terminal_json"] if "workspace_terminal_json" in row.keys() else None
        terminal_digest = row["workspace_terminal_sha256"] if "workspace_terminal_sha256" in row.keys() else None
        terminal_workspace = None
        if any(value is not None for value in (terminal_head, terminal_raw, terminal_digest)):
            if any(value is None for value in (terminal_head, terminal_raw, terminal_digest)):
                raise CorruptSchemaError("workspace terminal fact is incomplete")
            terminal_workspace = _decode_workspace_baseline(str(terminal_raw), str(terminal_digest))
        turn_started_at = row["turn_started_at"] if "turn_started_at" in row.keys() else None
        return ExecutionIntegrityRecord(
            RunId(row["run_id"]),
            MilestoneId(row["milestone_id"]),
            str(row["provenance"]),
            (_sha256(str(native_profile), field_name="native profile digest") if native_profile is not None else None),
            (
                _sha256(str(native_compatibility), field_name="native compatibility digest")
                if native_compatibility is not None
                else None
            ),
            effective_permission,
            (
                _sha256(str(permission_digest), field_name="effective native permission digest")
                if permission_digest is not None
                else None
            ),
            (_git_sha(str(baseline_head), field_name="workspace baseline HEAD") if baseline_head is not None else None),
            baseline,
            (
                _sha256(str(baseline_digest), field_name="workspace baseline digest")
                if baseline_digest is not None
                else None
            ),
            (_git_sha(str(terminal_head), field_name="workspace terminal HEAD") if terminal_head is not None else None),
            terminal_workspace,
            (
                _sha256(str(terminal_digest), field_name="workspace terminal digest")
                if terminal_digest is not None
                else None
            ),
            str(turn_started_at) if turn_started_at is not None else None,
            (
                _sha256(str(row["git_authority_before_sha256"]), field_name="Git authority before digest")
                if row["git_authority_before_sha256"] is not None
                else None
            ),
            (
                _sha256(str(row["git_authority_after_sha256"]), field_name="Git authority after digest")
                if row["git_authority_after_sha256"] is not None
                else None
            ),
            str(row["created_at"]),
            str(row["updated_at"]),
        )
