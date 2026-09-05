from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

import codex_flow.controller as controller_module
import codex_flow.harness as harness_module
from codex_flow import worker as worker_module
from codex_flow.backends.codex_sdk import ResponseChainInvalid, ThreadInspection, ThreadInspectionKind
from codex_flow.contracts import (
    ModelFacingControllerAction,
    ModelFacingControllerActionBundle,
    PluginCapabilitySnapshot,
    PluginReadiness,
    PluginRequirement,
    model_facing_result_schema_sha256,
)
from codex_flow.controller import Controller, git_authority_snapshot, protected_paths_digest
from codex_flow.domain import (
    AcceptanceMode,
    CandidateDisposition,
    CandidateRecord,
    CompatibilityRebind,
    ControllerActionKind,
    ControllerClaimantKind,
    ControllerDecisionId,
    ControllerDecisionState,
    ControllerGenerationState,
    ControllerGenerationStatus,
    ExecutionCapsule,
    ExecutionStatus,
    FindingCausalClass,
    Generation,
    LifecyclePhase,
    LiveTurnKeyframe,
    MilestoneId,
    NativePermissionAuthority,
    NativePermissionMode,
    ReasonCode,
    ReasoningEffort,
    RecoveryActionKind,
    RetryBudgetChange,
    RetryFailureClass,
    ReviewFinding,
    ReviewResult,
    RoleId,
    RunId,
    Severity,
    TerminalFailureAfterIdentity,
    ThreadIdentity,
    ValidationSpec,
    WorkerResultRejectionCode,
    WorkflowReason,
    WorkflowState,
    WorkspaceMode,
)
from codex_flow.harness import (
    QUEUE_TRIGGERING_OPERATIONS,
    FailedSpawnTerminalizationError,
    HarnessError,
    WorkerCompatibilityDrift,
    WorkerResultRejected,
    WorkflowHarness,
    process_birth_identity,
)
from codex_flow.ipc import IpcError, IpcTransportError, decode_frame, encode_frame
from codex_flow.ledger import (
    CorruptSchemaError,
    Ledger,
    LedgerError,
    StaleWriter,
    UnsupportedSchemaVersion,
    utc_now,
)
from codex_flow.plugin_capabilities import PluginCapabilityError, discover_plugin_capabilities
from codex_flow.worker import (
    WORKER_EXIT_RESPONSE_CHAIN_INVALID,
    WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY,
    WORKER_EXIT_TERMINAL_AFTER_IDENTITY,
    WORKER_EXIT_TRANSIENT_AFTER_IDENTITY,
    ResultTransportAfterIdentity,
    WorkerError,
    _resolve_shared_native_home,
    recovery_continuation_prompt,
)
from codex_flow.worktrees import WorktreeError

DISPATCH = "run/milestone/executor/1"


class _ExitedChild:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code

    def poll(self) -> int:
        return self.return_code


class _LiveChild:
    """Production-shaped child stub that remains live for liveness binding."""

    pid = os.getpid()

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None


class _StoppingChild:
    """Exact owned child that exits when the harness requests a stop."""

    pid = os.getpid()

    def __init__(self) -> None:
        self.return_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15

    def wait(self, *, timeout: float) -> int:
        assert timeout > 0
        assert self.return_code is not None
        return self.return_code

    def kill(self) -> None:
        self.return_code = -9


def test_worker_uses_standard_codex_home_without_exported_override() -> None:
    with TemporaryDirectory() as directory:
        fallback = Path(directory)
        standard = fallback / ".codex"
        standard.mkdir()

        assert _resolve_shared_native_home(None, fallback_home=fallback) == standard
        assert _resolve_shared_native_home("", fallback_home=fallback) == standard
        with pytest.raises(WorkerError, match="shared native Codex home is unavailable"):
            _resolve_shared_native_home(None, fallback_home=fallback / "missing")


def test_controller_generation_ipc_exposes_the_raw_sdk_thread_id() -> None:
    generation = ControllerGenerationStatus(
        ControllerDecisionId("decision/run/milestone/executor/1/checkpoint/1"),
        Generation(1),
        "controller-lineage",
        ControllerGenerationState.ACTIVE,
        controller_thread_id=ThreadIdentity("controller-thread-raw"),
    )

    payload = WorkflowHarness._controller_generation_payload(generation)

    assert payload["controller_thread_id"] == "controller-thread-raw"
    assert "ThreadIdentity(" not in json.dumps(payload, sort_keys=True)


def _queued_ledger(root: Path) -> Ledger:
    ledger = Ledger(root / "workflow.db")
    ledger.create_run("run")
    ledger.create_milestone("run", "milestone")
    ledger.claim_dispatch("run", "milestone", "executor", 1)
    ledger.enqueue_dispatch(
        DISPATCH,
        backend="sdk_headless",
        capsule_json='{"model":"gpt-test","permission_mode":"inherit_native","prompt":"bounded"}\n',
        route_json=(
            '{"leaf_worker_policy":{"agents.enabled":false,"config_overrides":'
            '["agents.enabled=false","features.multi_agent=false"],"features.multi_agent":false},'
            '"model":"gpt-test","role":"executor"}'
        ),
        workspace_path=root,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    return ledger


def _production_queued_ledger(root: Path) -> Ledger:
    """Build a queue row with the native facts required by real spawning."""

    ledger = _queued_ledger(root)
    route = {
        "effective_permission": {
            "approval_policy": "never",
            "mode": "inherit_native",
            "monotonic": True,
            "sandbox_mode": "workspace-write",
        },
        "leaf_worker_policy": {
            "agents.enabled": False,
            "config_overrides": ["agents.enabled=false", "features.multi_agent=false"],
            "features.multi_agent": False,
        },
        "native_compatibility_sha256": None,
        "native_profile_sha256": None,
    }
    ledger._db().execute(
        "UPDATE dispatch_queue SET route_json = ? WHERE dispatch_id = ?",
        (json.dumps(route, sort_keys=True), DISPATCH),
    )
    ledger._db().commit()
    return ledger


def _prepare_idle_continuation(ledger: Ledger, root: Path, epoch: int) -> None:
    """Move one production-shaped dispatch to an eligible idle continuation."""

    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    token = "worker-token"
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
    ledger.issue_attempt_capability(
        DISPATCH,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=root,
        backend="sdk_headless",
        token_sha256=token_sha256,
        expires_at="9999-12-31T23:59:59Z",
    )
    ledger.bind_worker_liveness(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        pid=os.getpid(),
        process_birth_identity="idle-worker",
        lease_token_sha256=token_sha256,
    )
    ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
    ledger._db().commit()
    ledger.mark_worker_exit(DISPATCH, pid=os.getpid(), process_birth_identity="idle-worker")
    ledger.record_recovery_inspection(
        DISPATCH,
        kind="idle_no_result",
        thread_id="old-thread",
        next_eligible_at="2000-01-01T00:00:00Z",
    )


def _configured_harness(ledger: Ledger, root: Path, epoch: int) -> WorkflowHarness:
    """Attach a harness instance to the fixture's explicitly opened ledger."""

    runtime_root = root / "runtime"
    runtime_root.mkdir()
    runtime_root.chmod(0o700)
    harness = object.__new__(WorkflowHarness)
    harness.state_root = root
    harness.state_dir = root
    harness.runtime_root = runtime_root
    harness.socket_path = runtime_root / "harness.sock"
    harness.lease_seconds = 30.0
    harness.worker_command = ("codex-flow-worker",)
    harness._wake_delivery = None
    harness._thread_inspector = None
    harness.ledger = ledger
    harness.owner_nonce = "test-owner"
    harness.owner_nonce_sha256 = hashlib.sha256(b"test-owner").hexdigest()
    harness.epoch = epoch
    harness._socket = None
    harness._children = {}
    harness._resumed_children = set()
    harness._stop = False
    harness._next_checkpoint_deadline = None
    harness._next_renewal_monotonic = None
    harness.controller_command = ("codex-flow-controller",)
    harness._controller_children = {}
    return harness


def _control_capsule(root: Path) -> ExecutionCapsule:
    return ExecutionCapsule(
        2,
        RunId("control-run"),
        MilestoneId("control-milestone"),
        root,
        WorkspaceMode.CURRENT_CHECKOUT,
        root,
        "main",
        "a" * 40,
        "control-lifecycle",
        ("owned.txt",),
        (),
        ValidationSpec(("true",), 5),
        "gpt-test",
        ReasoningEffort.MEDIUM,
        "complete the bounded control milestone",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        permission_mode=NativePermissionMode.READ_ONLY,
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )


def _control_candidate(root: Path, commit_sha: str) -> CandidateRecord:
    return CandidateRecord(
        CandidateDisposition.VERIFIED_COMMIT,
        root,
        commit_sha=commit_sha,
        workspace_head=commit_sha,
    )


def _control_review(
    review_id: str,
    role: str,
    candidate_sha: str,
    mode: AcceptanceMode,
    *,
    accepted: bool = True,
    findings: tuple[ReviewFinding, ...] = (),
) -> ReviewResult:
    return ReviewResult(
        review_id,
        RoleId(role),
        accepted,
        findings,
        candidate_sha,
        acceptance_mode=mode,
    )


def _write_test_native_profile(home: Path, catalog: Path) -> None:
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    for name in ("memories", "plugins", "skills"):
        (home / name).mkdir(mode=0o700)
    catalog.write_text('{"fetched_at":"2026-08-24T00:00:00Z","client_version":"0.147.0","models":[]}\n')
    catalog.chmod(0o600)
    (home / "config.toml").write_text(
        f'''model_provider = "codex-lb"
model_catalog_json = "{catalog}"
personality = "pragmatic"
approval_policy = "never"
approvals_reviewer = "user"
sandbox_mode = "danger-full-access"

[model_providers.codex-lb]
name = "openai"
base_url = "http://127.0.0.1:2455/backend-api/codex"
wire_api = "responses"
env_key = "CODEX_LB_API_KEY"
requires_openai_auth = true
supports_websockets = true

[agents]
enabled = true

[features]
memories = true

[hooks]

[marketplaces]

[mcp_servers]

[plugins]

[profiles]

[projects]

[shell_environment_policy]

[skills]
'''
    )
    (home / "config.toml").chmod(0o600)
    (home / "auth.json").write_text("{}\n")
    (home / "auth.json").chmod(0o600)


def test_control_acceptance_waits_for_all_authorities_and_repairs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
    capsule = _control_capsule(tmp_path)
    first_sha = "b" * 40
    second_sha = "c" * 40
    harness.ledger.create_run(capsule.run_id)
    harness.ledger.create_milestone(capsule.run_id, capsule.milestone_id)
    harness.ledger.claim_dispatch(capsule.run_id, capsule.milestone_id, "executor", 1)
    harness.ledger.record_control_executor_result(
        capsule.run_id,
        capsule.milestone_id,
        candidate=_control_candidate(tmp_path, first_sha),
        terminal_status="completed",
        dispatch_id="control-run/control-milestone/executor/1",
        result_sha256="d" * 64,
    )
    queued: list[tuple[str, int, str | None]] = []

    def queue(_capsule: ExecutionCapsule, **values: object) -> None:
        queued.append((str(values["role"]), int(values["generation"]), values.get("candidate_sha")))

    monkeypatch.setattr(harness, "_enqueue_control_dispatch", queue)
    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=first_sha,
        dispatch_id="control-run/control-milestone/code-reviewer/1",
        generation=1,
        result=_control_review(
            "objective-rejected",
            "code-reviewer",
            first_sha,
            AcceptanceMode.OBJECTIVE,
            accepted=False,
            findings=(
                ReviewFinding(
                    "control-p1",
                    FindingCausalClass.IMPLEMENTATION,
                    Severity.P1,
                    True,
                    "repair the rejected implementation",
                    {"candidate": first_sha},
                    "objective acceptance",
                ),
            ),
        ),
    )
    harness._advance_control_acceptance(capsule, first_sha, 1)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.REVIEWING
    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=first_sha,
        dispatch_id="control-run/control-milestone/architecture-reviewer/1",
        generation=1,
        result=_control_review(
            "architecture-accepted", "architecture-reviewer", first_sha, AcceptanceMode.ARCHITECTURE
        ),
    )
    harness._advance_control_acceptance(capsule, first_sha, 1)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.REPAIR_REQUIRED
    assert queued == [("executor", 2, first_sha)]

    harness.ledger.claim_program_repair_dispatch(capsule.run_id, capsule.milestone_id, generation=2)
    repair_raw = json.dumps(
        {
            "schema_version": 1,
            "status": "completed",
            "summary": "bounded repair",
            "changed_surfaces": [],
            "validations": [],
            "durable_status": "completed",
            "next_action": None,
            "blocker": None,
        },
        separators=(",", ":"),
    )
    repair_digest = hashlib.sha256(repair_raw.encode()).hexdigest()
    harness.ledger.enqueue_dispatch(
        "control-run/control-milestone/executor/2",
        backend="sdk_headless",
        capsule_json='{"model":"test","prompt":"repair"}',
        action_json=json.dumps(
            {"candidate_sha": first_sha, "finding_ids": ["control-p1"], "repair_generation": 2},
            sort_keys=True,
            separators=(",", ":"),
        ),
        route_json="{}",
        workspace_path=tmp_path,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    with harness.ledger._transaction():
        harness.ledger._db().execute(
            "UPDATE dispatch_queue SET state = 'result_submitted', raw_result_json = ?, raw_result_sha256 = ?, "
            "terminal_status = 'completed' WHERE dispatch_id = 'control-run/control-milestone/executor/2'",
            (repair_raw, repair_digest),
        )
        harness.ledger._record_dispatch_terminal_integrity_in_transaction(
            "control-run/control-milestone/executor/2",
            result_sha256=repair_digest,
            terminal_status="completed",
            terminal_integrity={
                "workspace_terminal_head_sha": second_sha,
                "workspace_terminal": (),
                "git_authority_sha256": "f" * 64,
                "protected_paths_sha256": "a" * 64,
                "captured_at": utc_now(),
            },
        )
    monkeypatch.setattr(harness, "_assert_control_repair_terminal_authority", lambda *_args, **_kwargs: None)

    harness.ledger.record_control_executor_result(
        capsule.run_id,
        capsule.milestone_id,
        candidate=_control_candidate(tmp_path, second_sha),
        terminal_status="completed",
        dispatch_id="control-run/control-milestone/executor/2",
        result_sha256=repair_digest,
    )
    facts = harness.ledger.review_lifecycle(capsule.run_id, capsule.milestone_id)
    assert any(fact.kind == "candidate_superseded" for fact in facts)
    assert any(fact.kind == "reviews_invalidated" for fact in facts)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.REVIEWING
    monkeypatch.setattr(
        harness_module,
        "load_workflow_config",
        lambda _path: SimpleNamespace(derive_authorities=lambda _modes: None),
    )
    harness._queue_control_reviews(capsule, second_sha, generation=2)
    assert queued[-2:] == [
        ("code-reviewer", 2, second_sha),
        ("architecture-reviewer", 2, second_sha),
    ]

    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=second_sha,
        dispatch_id="control-run/control-milestone/code-reviewer/2",
        generation=2,
        result=_control_review(
            "objective-rejected-again", "code-reviewer", second_sha, AcceptanceMode.OBJECTIVE, accepted=False
        ),
    )
    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=second_sha,
        dispatch_id="control-run/control-milestone/architecture-reviewer/2",
        generation=2,
        result=_control_review(
            "architecture-accepted-again", "architecture-reviewer", second_sha, AcceptanceMode.ARCHITECTURE
        ),
    )
    harness._advance_control_acceptance(capsule, second_sha, 2)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.FAILED
    assert not any(role == "executor" and generation == 3 for role, generation, _ in queued)
    assert any(
        fact.kind == "re_review_rejected"
        for fact in harness.ledger.review_lifecycle(capsule.run_id, capsule.milestone_id)
    )
    harness.close()


def test_generation_two_result_terminalization_is_atomic_without_integrity(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "workflow.db")
    ledger.create_run("control-run")
    ledger.create_milestone("control-run", "control-milestone")
    ledger.claim_dispatch("control-run", "control-milestone", "executor", 1)
    ledger.record_control_executor_result(
        "control-run",
        "control-milestone",
        candidate=_control_candidate(tmp_path, "a" * 40),
        terminal_status="completed",
        dispatch_id="control-run/control-milestone/executor/1",
        result_sha256="b" * 64,
    )
    ledger.record_review_transition(
        "control-run",
        "control-milestone",
        WorkflowState.REPAIR_REQUIRED,
        expected_state=WorkflowState.REVIEWING,
        phase=LifecyclePhase.REPAIR,
        kind="repair_requested",
        data={
            "candidate_sha": "a" * 40,
            "finding_ids": ["terminal-integrity"],
            "same_owner": True,
            "repair_generation": 2,
        },
    )
    ledger.claim_program_repair_dispatch("control-run", "control-milestone", generation=2)
    dispatch_id = "control-run/control-milestone/executor/2"
    ledger.enqueue_dispatch(
        dispatch_id,
        backend="sdk_headless",
        capsule_json='{"model":"test","prompt":"repair"}',
        action_json=json.dumps(
            {"candidate_sha": "a" * 40, "finding_ids": ["terminal-integrity"], "repair_generation": 2},
            sort_keys=True,
            separators=(",", ":"),
        ),
        route_json="{}",
        workspace_path=tmp_path,
        result_contract_sha256=model_facing_result_schema_sha256(),
    )
    epoch = int(_harness(ledger, tmp_path)["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="c" * 64)
    ledger.set_queue_state(dispatch_id, "starting", epoch=epoch)
    token = "repair-token"
    ledger.issue_attempt_capability(
        dispatch_id,
        generation=2,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
        expires_at="9999-12-31T23:59:59Z",
    )

    with pytest.raises(StaleWriter, match="lacks dispatch terminal integrity"):
        ledger.commit_queue_result(
            dispatch_id,
            generation=2,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )

    queue = ledger.queue_dispatch(dispatch_id)
    assert queue["state"] == "starting"
    assert queue["raw_result_sha256"] is None
    assert ledger._db().execute("SELECT COUNT(*) FROM dispatch_terminal_integrity").fetchone()[0] == 0
    capability = ledger.worker_attempt_capability(dispatch_id, generation=2, attempt=1, token=token)
    assert capability["consumed_at"] is None
    with pytest.raises(StaleWriter, match="capture is stale"):
        ledger.commit_queue_result(
            dispatch_id,
            generation=2,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
            terminal_integrity={
                "workspace_terminal_head_sha": "c" * 40,
                "workspace_terminal": (),
                "git_authority_sha256": "d" * 64,
                "protected_paths_sha256": "e" * 64,
                "captured_at": "2000-01-01T00:00:00.000000Z",
            },
        )
    assert ledger.queue_dispatch(dispatch_id)["state"] == "starting"
    assert ledger.worker_attempt_capability(dispatch_id, generation=2, attempt=1, token=token)["consumed_at"] is None
    ledger.close()


def test_control_acceptance_promotes_only_after_every_fresh_authority_accepts(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    capsule = _control_capsule(tmp_path)
    candidate_sha = "d" * 40
    harness.ledger.create_run(capsule.run_id)
    harness.ledger.create_milestone(capsule.run_id, capsule.milestone_id)
    harness.ledger.claim_dispatch(capsule.run_id, capsule.milestone_id, "executor", 1)
    harness.ledger.record_control_executor_result(
        capsule.run_id,
        capsule.milestone_id,
        candidate=_control_candidate(tmp_path, candidate_sha),
        terminal_status="completed",
        dispatch_id="control-run/control-milestone/executor/1",
        result_sha256="e" * 64,
    )
    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=candidate_sha,
        dispatch_id="control-run/control-milestone/code-reviewer/1",
        generation=1,
        result=_control_review("objective-accepted", "code-reviewer", candidate_sha, AcceptanceMode.OBJECTIVE),
    )
    harness._advance_control_acceptance(capsule, candidate_sha, 1)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.REVIEWING
    harness.ledger.record_control_review(
        capsule.run_id,
        capsule.milestone_id,
        candidate_sha=candidate_sha,
        dispatch_id="control-run/control-milestone/architecture-reviewer/1",
        generation=1,
        result=_control_review(
            "architecture-accepted", "architecture-reviewer", candidate_sha, AcceptanceMode.ARCHITECTURE
        ),
    )
    harness._advance_control_acceptance(capsule, candidate_sha, 1)
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.ACCEPTED
    assert any(
        fact.kind == "promotion_accepted" and fact.data["candidate_sha"] == candidate_sha
        for fact in harness.ledger.review_lifecycle(capsule.run_id, capsule.milestone_id)
    )
    harness.close()


def _terminal_authority_harness(
    root: Path,
    *,
    terminal_head: str = "a" * 40,
    terminal_git: str = "b" * 64,
    terminal_protected: str = "c" * 64,
) -> tuple[WorkflowHarness, ExecutionCapsule]:
    """Build a narrow harness seam for terminal-authority adversaries."""

    integrity = SimpleNamespace(
        workspace_terminal_head_sha=terminal_head,
        workspace_terminal=(("owned.txt", "signature"),),
        git_authority_after_sha256=terminal_git,
    )
    ledger = SimpleNamespace(
        get_execution=lambda _run, _milestone: SimpleNamespace(
            status=ExecutionStatus.COMPLETED,
            protected_after_sha256=terminal_protected,
        ),
        get_execution_integrity=lambda _run, _milestone: integrity,
    )
    harness = object.__new__(WorkflowHarness)
    harness.state_root = root
    harness.ledger = ledger
    harness._worktrees = SimpleNamespace()
    return harness, _control_capsule(root)


def test_control_rejects_head_advancing_after_terminalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness, capsule = _terminal_authority_harness(tmp_path)
    calls = iter(
        (
            SimpleNamespace(returncode=0, stdout="d" * 40 + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        )
    )
    monkeypatch.setattr(harness_module.subprocess, "run", lambda *args, **kwargs: next(calls))

    with pytest.raises(WorktreeError, match="HEAD advanced"):
        harness._assert_control_terminal_authority(capsule, candidate_sha="a" * 40, require_terminal_facts=True)


def test_control_rejects_dirty_workspace_bytes_after_terminalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, capsule = _terminal_authority_harness(tmp_path)
    calls = iter(
        (
            SimpleNamespace(returncode=0, stdout="a" * 40 + "\n"),
            SimpleNamespace(returncode=0, stdout=" M owned.txt\n"),
        )
    )
    monkeypatch.setattr(harness_module.subprocess, "run", lambda *args, **kwargs: next(calls))

    with pytest.raises(WorktreeError, match="dirty bytes"):
        harness._assert_control_terminal_authority(capsule, candidate_sha="a" * 40, require_terminal_facts=True)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("candidate", "durable terminal HEAD"),
        ("workspace", "workspace bytes changed"),
        ("git", "Git authority changed"),
        ("protected", "protected paths changed"),
    ),
)
def test_control_terminal_authority_rejects_each_named_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    harness, capsule = _terminal_authority_harness(tmp_path)
    if drift == "candidate":
        with pytest.raises(WorktreeError, match=message):
            harness._assert_control_terminal_authority(
                capsule,
                candidate_sha="d" * 40,
                require_terminal_facts=True,
            )
        return

    calls = iter(
        (
            SimpleNamespace(returncode=0, stdout="a" * 40 + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        )
    )
    monkeypatch.setattr(harness_module.subprocess, "run", lambda *args, **kwargs: next(calls))

    class SnapshotController:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _owned_workspace_snapshot(self, _capsule: ExecutionCapsule) -> tuple[tuple[str, str], ...]:
            return (("owned.txt", "drift" if drift == "workspace" else "signature"),)

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller_module, "Controller", SnapshotController)
    monkeypatch.setattr(
        controller_module,
        "git_authority_snapshot",
        lambda _path: SimpleNamespace(sha256="d" * 64 if drift == "git" else "b" * 64),
    )
    monkeypatch.setattr(
        controller_module,
        "protected_paths_digest",
        lambda _path, _paths: "d" * 64 if drift == "protected" else "c" * 64,
    )

    with pytest.raises(WorktreeError, match=message):
        harness._assert_control_terminal_authority(capsule, candidate_sha="a" * 40, require_terminal_facts=True)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("result", "stale or conflicting"),
        ("candidate", "stale or conflicting"),
        ("dirty", "dirty bytes"),
        ("workspace", "workspace bytes changed"),
        ("git", "Git authority changed"),
        ("protected", "protected paths changed"),
    ),
)
def test_generation_two_terminal_integrity_rejects_post_ingestion_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    dispatch_id = "control-run/control-milestone/executor/2"
    candidate_sha = "a" * 40
    result_sha256 = "b" * 64
    integrity = {
        "dispatch_id": dispatch_id,
        "result_sha256": "c" * 64 if drift == "result" else result_sha256,
        "workspace_terminal_head_sha": "d" * 40 if drift == "candidate" else candidate_sha,
        "workspace_terminal": (("owned.txt", "file:" + "e" * 64),),
        "workspace_terminal_sha256": "f" * 64,
        "git_authority_sha256": "1" * 64,
        "protected_paths_sha256": "2" * 64,
        "captured_at": utc_now(),
    }
    queue = {
        "run_id": "control-run",
        "milestone_id": "control-milestone",
        "role": "executor",
        "generation": 2,
        "terminal_status": "completed",
        "raw_result_sha256": result_sha256,
    }
    harness = object.__new__(WorkflowHarness)
    harness.state_root = tmp_path
    harness.ledger = SimpleNamespace(
        dispatch_terminal_integrity=lambda _dispatch: integrity,
        queue_dispatch=lambda _dispatch: queue,
    )
    harness._worktrees = SimpleNamespace()
    capsule = _control_capsule(tmp_path)
    calls = iter(
        (
            SimpleNamespace(returncode=0, stdout=candidate_sha + "\n"),
            SimpleNamespace(returncode=0, stdout=" M owned.txt\n" if drift == "dirty" else ""),
        )
    )
    monkeypatch.setattr(harness_module.subprocess, "run", lambda *args, **kwargs: next(calls))

    class SnapshotController:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def _owned_workspace_snapshot(self, _capsule: ExecutionCapsule) -> tuple[tuple[str, str], ...]:
            signature = "file:" + ("3" * 64 if drift == "workspace" else "e" * 64)
            return (("owned.txt", signature),)

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller_module, "Controller", SnapshotController)
    monkeypatch.setattr(
        controller_module,
        "git_authority_snapshot",
        lambda _path: SimpleNamespace(sha256="4" * 64 if drift == "git" else "1" * 64),
    )
    monkeypatch.setattr(
        controller_module,
        "protected_paths_digest",
        lambda _path, _paths: "5" * 64 if drift == "protected" else "2" * 64,
    )

    with pytest.raises(WorktreeError, match=message):
        harness._assert_control_repair_terminal_authority(
            capsule,
            dispatch_id=dispatch_id,
            candidate_sha=candidate_sha,
            result_sha256=result_sha256,
        )


def test_control_dispatch_reuse_accepts_matching_candidate_role_and_generation(tmp_path: Path) -> None:
    capsule = _control_capsule(tmp_path)
    dispatch_id = "control-run/control-milestone/code-reviewer/1"
    row = {
        "dispatch_id": dispatch_id,
        "run_id": str(capsule.run_id),
        "milestone_id": str(capsule.milestone_id),
        "role": "code-reviewer",
        "generation": 1,
        "backend": "sdk_headless",
        "workspace_path": os.fspath(capsule.workspace_path.resolve()),
        "action_json": json.dumps(
            {"candidate_sha": "a" * 40, "review_role": "code-reviewer", "generation": 1},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    harness = object.__new__(WorkflowHarness)
    harness.ledger = SimpleNamespace(queue_dispatch=lambda _dispatch: row)

    harness._enqueue_control_dispatch(
        capsule,
        role="code-reviewer",
        generation=1,
        candidate_sha="a" * 40,
    )


def test_control_dispatch_reuse_rejects_conflicting_repair_findings(tmp_path: Path) -> None:
    capsule = _control_capsule(tmp_path)
    dispatch_id = "control-run/control-milestone/executor/2"
    row = {
        "dispatch_id": dispatch_id,
        "run_id": str(capsule.run_id),
        "milestone_id": str(capsule.milestone_id),
        "role": "executor",
        "generation": 2,
        "backend": "sdk_headless",
        "workspace_path": os.fspath(capsule.workspace_path.resolve()),
        "action_json": json.dumps(
            {"candidate_sha": "a" * 40, "finding_ids": ["finding-old"], "repair_generation": 2},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    harness = object.__new__(WorkflowHarness)
    harness.ledger = SimpleNamespace(queue_dispatch=lambda _dispatch: row)

    with pytest.raises(WorktreeError, match="action context conflicts"):
        harness._enqueue_control_dispatch(
            capsule,
            role="executor",
            generation=2,
            candidate_sha="a" * 40,
            finding_ids=("finding-new",),
        )


def test_control_restart_adopts_exact_retained_lineage_without_executor_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = Path(__file__).parents[1]
    repository = tmp_path / "repo"
    subprocess.run(("git", "clone", "--quiet", os.fspath(source_repository), os.fspath(repository)), check=True)
    assert (
        subprocess.run(
            ("git", "show", "-s", "--format=%P", "47a8fa3d67575ef00662f1b5693efaf45c7b52bd"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "507645ea5e788eaa4ec803b142419655e1c6dd69"
    )
    assert (
        subprocess.run(
            ("git", "show", "-s", "--format=%P", "977d8f5c8c55459625dbff8133c262e12f91bba0"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "47a8fa3d67575ef00662f1b5693efaf45c7b52bd"
    )
    branch = subprocess.run(
        ("git", "branch", "--show-current"), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    capsule = ExecutionCapsule(
        2,
        RunId("safe-refresh-control"),
        MilestoneId("safe-refresh-final-closure"),
        repository,
        WorkspaceMode.CURRENT_CHECKOUT,
        repository,
        branch,
        "507645ea5e788eaa4ec803b142419655e1c6dd69",
        "control-migration",
        (
            "src/codex_flow/service.py",
            "tests/test_service_lifecycle.py",
            "docs/reviews/evidence",
        ),
        (),
        ValidationSpec(("true",), 5),
        "gpt-test",
        ReasoningEffort.MEDIUM,
        "adopt the retained candidate and return one typed result",
        {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        permission_mode=NativePermissionMode.READ_ONLY,
        acceptance_modes=(AcceptanceMode.OBJECTIVE, AcceptanceMode.ARCHITECTURE),
    )
    planner = Controller(repository)
    planner.plan(capsule)
    planner.close()

    harness = WorkflowHarness(repository)
    harness.ledger.acquire_workspace_lease(capsule)
    harness.ledger.record_native_profile(
        capsule.run_id,
        capsule.milestone_id,
        str("1" * 64),
        str("2" * 64),
        NativePermissionAuthority(NativePermissionMode.READ_ONLY, "read-only", "never"),
        capsule.base_sha,
        (),
        str("3" * 64),
    )
    harness.ledger.claim_dispatch(capsule.run_id, capsule.milestone_id, "executor", 1)
    now = utc_now()
    snapshot_controller = Controller(repository)
    terminal_workspace = snapshot_controller._owned_workspace_snapshot(capsule)
    snapshot_controller.close()
    terminal_workspace_json = json.dumps(terminal_workspace, separators=(",", ":"))
    terminal_workspace_sha = hashlib.sha256(terminal_workspace_json.encode("utf-8")).hexdigest()
    terminal_git_sha = git_authority_snapshot(repository).sha256
    terminal_protected_sha = protected_paths_digest(repository, capsule.protected_paths)
    result_json = json.dumps({"status": "completed"}, separators=(",", ":"))
    with harness.ledger._transaction():
        harness.ledger._db().execute(
            "UPDATE executions SET status = 'completed', checkpoint = 'result_durable', thread_id = ?, turn_id = ?, "
            "result_json = ?, validation_argv_json = ?, validation_exit_code = 0, validation_stdout_sha256 = ?, "
            "validation_stderr_sha256 = ?, validation_timed_out = 0, validation_duration_seconds = 0.01, "
            "protected_after_sha256 = ?, updated_at = ? WHERE run_id = ? AND milestone_id = ?",
            (
                "historical-thread",
                "historical-turn",
                result_json,
                json.dumps(["true"], separators=(",", ":")),
                "5" * 64,
                "6" * 64,
                terminal_protected_sha,
                now,
                str(capsule.run_id),
                str(capsule.milestone_id),
            ),
        )
        harness.ledger._db().execute(
            "UPDATE execution_integrity SET workspace_terminal_head_sha = ?, workspace_terminal_json = ?, "
            "workspace_terminal_sha256 = ?, turn_started_at = ?, git_authority_after_sha256 = ?, updated_at = ? "
            "WHERE run_id = ? AND milestone_id = ?",
            (
                "977d8f5c8c55459625dbff8133c262e12f91bba0",
                terminal_workspace_json,
                terminal_workspace_sha,
                now,
                terminal_git_sha,
                now,
                str(capsule.run_id),
                str(capsule.milestone_id),
            ),
        )
        harness.ledger._db().execute(
            "UPDATE milestones SET current_state = 'RUNNING', updated_at = ? WHERE run_id = ? AND milestone_id = ?",
            (now, str(capsule.run_id), str(capsule.milestone_id)),
        )
        harness.ledger._append_event_in_transaction(
            capsule.run_id,
            capsule.milestone_id,
            from_state=WorkflowState.STARTING,
            to_state=WorkflowState.RUNNING,
            event_type="state_transition",
            reason=None,
            dispatch_id=None,
            data=None,
        )
        harness.ledger._db().execute(
            "UPDATE milestones SET current_state = 'COMPLETED', updated_at = ? WHERE run_id = ? AND milestone_id = ?",
            (now, str(capsule.run_id), str(capsule.milestone_id)),
        )
        harness.ledger._append_event_in_transaction(
            capsule.run_id,
            capsule.milestone_id,
            from_state=WorkflowState.RUNNING,
            to_state=WorkflowState.COMPLETED,
            event_type="state_transition",
            reason=WorkflowReason(ReasonCode.TERMINAL_OUTCOME),
            dispatch_id=None,
            data=None,
        )
    monkeypatch.setenv("CODEX_HOME", os.fspath(tmp_path / "native-home"))
    monkeypatch.setenv("CODEX_LB_API_KEY", "test-only")
    _write_test_native_profile(tmp_path / "native-home", tmp_path / "native-home" / "models.json")

    adopted = harness.reconcile_control_execution(str(capsule.run_id), str(capsule.milestone_id))
    assert adopted.commit_sha == "977d8f5c8c55459625dbff8133c262e12f91bba0"
    assert harness.ledger.current_state(capsule.run_id, capsule.milestone_id) is WorkflowState.REVIEWING
    assert {str(row["dispatch_id"]) for row in harness.ledger.queue_dispatches()} == {
        "safe-refresh-control/safe-refresh-final-closure/code-reviewer/1",
        "safe-refresh-control/safe-refresh-final-closure/architecture-reviewer/1",
    }
    harness.close()

    restarted = WorkflowHarness(repository)
    replayed = restarted.reconcile_control_execution(str(capsule.run_id), str(capsule.milestone_id))
    assert replayed.commit_sha == adopted.commit_sha
    assert len(restarted.ledger.queue_dispatches()) == 2
    with pytest.raises(CorruptSchemaError):
        restarted.ledger.program_status(str(capsule.run_id))
    restarted.close()

    corrupted = WorkflowHarness(repository)
    with corrupted.ledger._transaction():
        corrupted.ledger._db().execute(
            "UPDATE execution_integrity SET git_authority_after_sha256 = ? WHERE run_id = ? AND milestone_id = ?",
            ("4" * 64, str(capsule.run_id), str(capsule.milestone_id)),
        )
    with pytest.raises(WorktreeError, match="Git authority changed"):
        corrupted.reconcile_control_execution(str(capsule.run_id), str(capsule.milestone_id))
    corrupted.close()


def test_harness_start_rejects_dangling_legacy_socket_entry_before_claim(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path)
    try:
        legacy = harness.runtime_root / "supervisor.sock"
        legacy.symlink_to(harness.runtime_root / "missing.sock")
        with pytest.raises(HarnessError, match="legacy supervisor socket path remains"):
            harness.acquire()
        assert harness.ledger.harness_authority() is None
    finally:
        harness.close()


def _compatibility_attention_ledger(
    root: Path,
    *,
    old_digest: str = "a" * 64,
    old_profile: str = "c" * 64,
) -> Ledger:
    ledger = _production_queued_ledger(root)
    route = json.loads(str(ledger.queue_dispatch(DISPATCH)["route_json"]))
    route["native_compatibility_sha256"] = old_digest
    route["native_profile_sha256"] = old_profile
    ledger._db().execute(
        "UPDATE dispatch_queue SET route_json = ? WHERE dispatch_id = ?",
        (json.dumps(route, sort_keys=True), DISPATCH),
    )
    ledger._db().execute(
        "UPDATE queue_bindings SET native_profile_sha256 = ?, native_compatibility_sha256 = ? WHERE dispatch_id = ?",
        (old_profile, old_digest, DISPATCH),
    )
    ledger._db().execute(
        "UPDATE recovery_state SET profile_sha256 = ? WHERE dispatch_id = ?",
        (old_profile, DISPATCH),
    )
    ledger._db().commit()
    ledger.mark_human_attention_required(
        DISPATCH,
        reason=f"native compatibility identity drift before worker launch: queued={old_digest}; current={'b' * 64}",
        failure_class="profile",
    )
    return ledger


def _identity_rebind(
    expected_compatibility_sha256: str = "a" * 64,
    proposed_compatibility_sha256: str = "b" * 64,
    expected_generation: int = 1,
    expected_attempt: int = 1,
    *,
    expected_profile_sha256: str = "c" * 64,
    proposed_profile_sha256: str = "d" * 64,
) -> CompatibilityRebind:
    return CompatibilityRebind(
        expected_compatibility_sha256,
        proposed_compatibility_sha256,
        expected_generation,
        expected_attempt,
        expected_profile_sha256,
        proposed_profile_sha256,
    )


def test_exact_compatibility_rebind_is_audited_without_launching_or_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        before = ledger.queue_dispatch(DISPATCH)
        rebind = _identity_rebind()

        action = ledger.apply_recovery_action(
            DISPATCH,
            action_id="authorized-installed-runtime-rebind",
            expected_revision=revision,
            action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
            reason="human authorized exact installed runtime compatibility",
            compatibility_rebind=rebind,
        )

        after = ledger.queue_dispatch(DISPATCH)
        assert action["compatibility_rebind_json"] == json.dumps(
            rebind.to_json(), sort_keys=True, separators=(",", ":")
        )
        assert after["state"] == before["state"] == "human_attention_required"
        assert after["generation"] == before["generation"] == 1
        assert after["attempt"] == before["attempt"] == 1
        assert after["thread_id"] is None
        assert ledger.worker_liveness(DISPATCH) is None
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert json.loads(str(after["route_json"]))["native_compatibility_sha256"] == "b" * 64
        assert json.loads(str(after["route_json"]))["native_profile_sha256"] == "d" * 64
        assert ledger.queue_binding(DISPATCH)["native_compatibility_sha256"] == "b" * 64
        assert ledger.queue_binding(DISPATCH)["native_profile_sha256"] == "d" * 64
        assert ledger.recovery_state(DISPATCH)["profile_sha256"] == "d" * 64
        assert ledger.retry_policy(DISPATCH)["revision"] == revision + 1

        retry = ledger.apply_recovery_action(
            DISPATCH,
            action_id="retry-after-exact-runtime-rebind",
            expected_revision=revision + 1,
            action_kind=RecoveryActionKind.RETRY,
            reason="one separately authorized dispatch retry",
        )
        assert retry["action_kind"] == "retry"
        queued = ledger.queue_dispatch(DISPATCH)
        assert queued["state"] == "queued"
        assert queued["attempt"] == 2
        assert json.loads(str(queued["route_json"]))["native_compatibility_sha256"] == "b" * 64
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        observed: dict[str, object] = {}

        def observe_rebound_identity(_row: dict[str, object], **facts: object) -> None:
            observed.update(facts)

        monkeypatch.setattr(harness, "_issue_and_spawn_verified_worker", observe_rebound_identity)
        assert harness.process_once() is True
        assert observed["native_compatibility_sha256"] == "b" * 64
        assert harness._children == {}
        ledger.close()


@pytest.mark.parametrize(
    ("revision_delta", "old_digest", "generation", "attempt", "message"),
    [
        (-1, "a" * 64, 1, 1, "revision is stale"),
        (0, "c" * 64, 1, 1, "old identity"),
        (0, "a" * 64, 2, 1, "worker identity is stale"),
        (0, "a" * 64, 1, 2, "worker identity is stale"),
    ],
)
def test_compatibility_rebind_rejects_stale_exact_facts_before_mutation(
    revision_delta: int,
    old_digest: str,
    generation: int,
    attempt: int,
    message: str,
) -> None:
    with TemporaryDirectory() as directory:
        ledger = _compatibility_attention_ledger(Path(directory))
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        before_queue = ledger.queue_dispatch(DISPATCH)
        before_binding = ledger.queue_binding(DISPATCH)
        with pytest.raises(StaleWriter, match=message):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="rejected-runtime-rebind",
                expected_revision=revision + revision_delta,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="bounded rejection test",
                compatibility_rebind=_identity_rebind(old_digest, "b" * 64, generation, attempt),
            )
        assert ledger.queue_dispatch(DISPATCH) == before_queue
        assert ledger.queue_binding(DISPATCH) == before_binding
        assert ledger._db().execute("SELECT COUNT(*) FROM recovery_controls").fetchone()[0] == 0
        ledger.close()


def test_compatibility_rebind_rejects_wrong_old_profile_before_mutation() -> None:
    with TemporaryDirectory() as directory:
        ledger = _compatibility_attention_ledger(Path(directory))
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        before_queue = ledger.queue_dispatch(DISPATCH)
        before_binding = ledger.queue_binding(DISPATCH)
        before_recovery = ledger.recovery_state(DISPATCH)
        with pytest.raises(StaleWriter, match="old identity tuple"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="wrong-old-profile-rebind",
                expected_revision=revision,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="reject mismatched profile authority",
                compatibility_rebind=_identity_rebind(expected_profile_sha256="e" * 64),
            )
        assert ledger.queue_dispatch(DISPATCH) == before_queue
        assert ledger.queue_binding(DISPATCH) == before_binding
        assert ledger.recovery_state(DISPATCH) == before_recovery
        ledger.close()


def test_compatibility_rebind_rejects_replay_conflict_and_active_worker() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        facts = _identity_rebind()
        ledger.apply_recovery_action(
            DISPATCH,
            action_id="one-shot-rebind",
            expected_revision=revision,
            action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
            reason="one shot",
            compatibility_rebind=facts,
        )
        with pytest.raises(StaleWriter, match="already applied"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="one-shot-rebind",
                expected_revision=revision,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="one shot",
                compatibility_rebind=facts,
            )
        with pytest.raises(StaleWriter, match="identity conflicts"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="one-shot-rebind",
                expected_revision=revision,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="conflicting reason",
                compatibility_rebind=facts,
            )
        ledger.close()

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        queued = ledger.queue_dispatch(DISPATCH)
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=str(queued["result_contract_sha256"]),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256="f" * 64,
        )
        with pytest.raises(StaleWriter, match="issued worker attempt"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="issued-attempt-rebind",
                expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="must reject issued attempt authority",
                compatibility_rebind=_identity_rebind(),
            )
        ledger.close()

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        ledger._db().execute(
            "INSERT INTO worker_liveness(dispatch_id, generation, attempt, pid, process_birth_identity, "
            "lease_token_sha256, lease_expires_at, last_seen_at, exited_at) VALUES (?, 1, 1, 1, 'active', ?, "
            "'9999-01-01T00:00:00Z', '2026-01-01T00:00:00Z', NULL)",
            (DISPATCH, "d" * 64),
        )
        ledger._db().commit()
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        with pytest.raises(StaleWriter, match="active worker lease"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="active-worker-rebind",
                expected_revision=revision,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="must reject active worker",
                compatibility_rebind=_identity_rebind(),
            )
        ledger.close()

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=decision.revision,
        )
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        with pytest.raises(StaleWriter, match="active controller turn or lease"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="active-controller-rebind",
                expected_revision=revision,
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="must reject active controller authority",
                compatibility_rebind=_identity_rebind(),
            )
        ledger.close()


def test_compatibility_rebind_revokes_only_an_unconsumed_exited_attempt() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        queued = ledger.queue_dispatch(DISPATCH)
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=str(queued["result_contract_sha256"]),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256="f" * 64,
        )
        ledger._db().execute(
            "INSERT INTO worker_liveness(dispatch_id, generation, attempt, pid, process_birth_identity, "
            "lease_token_sha256, lease_expires_at, last_seen_at, exited_at) VALUES (?, 1, 1, 1, 'exited', ?, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z')",
            (DISPATCH, "d" * 64),
        )
        ledger._db().commit()
        revision = int(ledger.retry_policy(DISPATCH)["revision"])

        ledger.apply_recovery_action(
            DISPATCH,
            action_id="rebind-after-exited-attempt",
            expected_revision=revision,
            action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
            reason="revoke the exact exited attempt before runtime identity rotation",
            compatibility_rebind=_identity_rebind(),
        )

        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert ledger.worker_liveness(DISPATCH)["exited_at"] == "2026-01-01T00:00:01Z"
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_dispatch(DISPATCH)["attempt"] == 1
        assert ledger.retry_policy(DISPATCH)["revision"] == revision + 1
        ledger.close()


def test_pre_worker_compatibility_drift_is_typed_profile_attention() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        ledger.apply_recovery_action(
            DISPATCH,
            action_id="reset-for-drift-test",
            expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
            action_kind=RecoveryActionKind.RETRY,
            reason="exercise typed drift classification",
        )
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        row = ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="e" * 64)
        assert row is not None
        drift = WorkerCompatibilityDrift(
            queued_profile_sha256="c" * 64,
            current_profile_sha256="d" * 64,
            queued_compatibility_sha256="a" * 64,
            current_compatibility_sha256="b" * 64,
        )
        with pytest.raises(WorkerCompatibilityDrift):
            try:
                raise drift
            except WorkerCompatibilityDrift as exc:
                harness._close_failed_spawn(row, (), failure=exc)
                raise
        policy = ledger.retry_policy(DISPATCH)
        assert policy["last_failure"] == "profile"
        assert policy["human_attention_reason"] == str(drift)
        assert "malformed" not in str(policy["human_attention_reason"])
        assert "profile/configuration identity drift" in str(policy["human_attention_reason"])
        ledger.close()


def test_spawn_checks_complete_verified_runtime_tuple_before_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = False

    class _DriftedProfile:
        profile_sha256 = "d" * 64
        worker_compatibility_sha256 = "a" * 64

        def verify_worker_sources(self) -> None:
            nonlocal verified
            verified = True

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        monkeypatch.setattr(harness_module.NativeProfileProjection, "load", lambda _home: _DriftedProfile())

        with pytest.raises(WorkerCompatibilityDrift, match="profile/configuration identity drift"):
            harness._issue_and_spawn_verified_worker(
                ledger.queue_dispatch(DISPATCH),
                capsule_value={},
                policy={},
                effective_permission={},
                native_profile_sha256="c" * 64,
                native_compatibility_sha256="a" * 64,
                plugin_requirements=(),
                plugin_snapshots=(),
                profile_home=root,
                recovery_continuation=False,
                created_paths=[],
            )
        assert verified is True
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert harness._children == {}
        ledger.close()


@pytest.mark.parametrize(
    ("proposed_profile", "proposed_compatibility"),
    [("c" * 64, "b" * 64), ("d" * 64, "a" * 64)],
)
def test_compatibility_rebind_permits_exactly_one_tuple_component_to_change(
    proposed_profile: str,
    proposed_compatibility: str,
) -> None:
    with TemporaryDirectory() as directory:
        ledger = _compatibility_attention_ledger(Path(directory))
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        ledger.apply_recovery_action(
            DISPATCH,
            action_id=f"single-component-{proposed_profile[0]}-{proposed_compatibility[0]}",
            expected_revision=revision,
            action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
            reason="authorize one changed runtime identity component",
            compatibility_rebind=_identity_rebind(
                proposed_compatibility_sha256=proposed_compatibility,
                proposed_profile_sha256=proposed_profile,
            ),
        )
        route = json.loads(str(ledger.queue_dispatch(DISPATCH)["route_json"]))
        binding = ledger.queue_binding(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        assert route["native_profile_sha256"] == binding["native_profile_sha256"] == proposed_profile
        assert recovery["profile_sha256"] == proposed_profile
        assert route["native_compatibility_sha256"] == binding["native_compatibility_sha256"] == proposed_compatibility
        ledger.close()


def test_compatibility_rebind_rejects_partial_tuple_total_noop_and_legacy_mutation() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        CompatibilityRebind("a" * 64, "b" * 64, 1, 1, "c" * 64, None)
    with pytest.raises(ValueError, match="identity change"):
        _identity_rebind(proposed_compatibility_sha256="a" * 64, proposed_profile_sha256="c" * 64)

    with TemporaryDirectory() as directory:
        ledger = _compatibility_attention_ledger(Path(directory))
        with pytest.raises(ValueError, match="complete runtime identity tuple"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="legacy-payload-cannot-mutate",
                expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
                action_kind=RecoveryActionKind.COMPATIBILITY_REBIND,
                reason="legacy receipt shape is read-only",
                compatibility_rebind=CompatibilityRebind("a" * 64, "b" * 64, 1, 1),
            )
        assert ledger._db().execute("SELECT COUNT(*) FROM recovery_controls").fetchone()[0] == 0
        ledger.close()


def test_harness_rebind_verifies_current_profile_before_ledger_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _VerifiedProfile:
        profile_sha256 = "d" * 64
        worker_compatibility_sha256 = "b" * 64

        def verify_worker_sources(self) -> None:
            return None

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        monkeypatch.setenv("CODEX_HOME", str(root))
        monkeypatch.setattr(harness_module.NativeProfileProjection, "load", lambda _home: _VerifiedProfile())
        revision = int(ledger.retry_policy(DISPATCH)["revision"])

        response = harness._recovery_action(
            {
                "version": 1,
                "operation": "recovery_action",
                "dispatch_id": DISPATCH,
                "action_id": "verified-harness-runtime-rebind",
                "expected_revision": revision,
                "action_kind": "compatibility_rebind",
                "reason": "human authorized exact installed runtime",
                "compatibility_rebind": _identity_rebind().to_json(),
            }
        )

        assert response["ok"] is True
        assert response["action"]["action_kind"] == "compatibility_rebind"  # type: ignore[index]
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_binding(DISPATCH)["native_compatibility_sha256"] == "b" * 64
        ledger.close()


def test_harness_rebind_preserves_stale_cas_classification_after_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _VerifiedProfile:
        profile_sha256 = "d" * 64
        worker_compatibility_sha256 = "b" * 64

        def verify_worker_sources(self) -> None:
            return None

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        monkeypatch.setenv("CODEX_HOME", str(root))
        monkeypatch.setattr(harness_module.NativeProfileProjection, "load", lambda _home: _VerifiedProfile())

        with pytest.raises(StaleWriter, match="revision is stale"):
            harness._recovery_action(
                {
                    "version": 1,
                    "operation": "recovery_action",
                    "dispatch_id": DISPATCH,
                    "action_id": "stale-verified-runtime-rebind",
                    "expected_revision": int(ledger.retry_policy(DISPATCH)["revision"]) - 1,
                    "action_kind": "compatibility_rebind",
                    "reason": "preserve durable CAS classification",
                    "compatibility_rebind": _identity_rebind().to_json(),
                }
            )
        assert ledger._db().execute("SELECT COUNT(*) FROM recovery_controls").fetchone()[0] == 0
        ledger.close()


@pytest.mark.parametrize("failure_kind", ("profile", "plugin"))
def test_harness_rebind_verification_failure_preserves_queue_authority(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _compatibility_attention_ledger(root)
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        monkeypatch.setenv("CODEX_HOME", str(root))
        if failure_kind == "profile":
            monkeypatch.setattr(
                harness_module.NativeProfileProjection,
                "load",
                lambda _home: (_ for _ in ()).throw(ValueError("unverified profile")),
            )
        else:
            monkeypatch.setattr(
                harness_module,
                "_verified_skill_input",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(PluginCapabilityError("plugin changed")),
            )
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        before_queue = ledger.queue_dispatch(DISPATCH)
        before_binding = ledger.queue_binding(DISPATCH)

        with pytest.raises(IpcError, match="could not be verified"):
            harness._recovery_action(
                {
                    "version": 1,
                    "operation": "recovery_action",
                    "dispatch_id": DISPATCH,
                    "action_id": f"rejected-{failure_kind}-runtime-rebind",
                    "expected_revision": revision,
                    "action_kind": "compatibility_rebind",
                    "reason": "must reject before mutation",
                    "compatibility_rebind": _identity_rebind().to_json(),
                }
            )
        assert ledger.queue_dispatch(DISPATCH) == before_queue
        assert ledger.queue_binding(DISPATCH) == before_binding
        assert ledger.retry_policy(DISPATCH)["revision"] == revision
        ledger.close()


def test_foreground_queue_event_contains_a_durably_closed_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        harness = _configured_harness(ledger, root, epoch)

        def failed_spawn() -> bool:
            ledger.mark_human_attention_required(
                DISPATCH,
                reason="worker spawn failed before a durable live-worker binding",
            )
            raise HarnessError("worker attempt artifacts already exist")

        monkeypatch.setattr(harness, "process_once", failed_spawn)

        assert harness._process_queue_event() is False
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        ledger.close()


def test_foreground_queue_event_does_not_hide_failed_spawn_terminalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        harness = _configured_harness(ledger, root, epoch)

        def uncertain_spawn() -> bool:
            raise FailedSpawnTerminalizationError("worker spawn failed before durable terminalization")

        monkeypatch.setattr(harness, "process_once", uncertain_spawn)

        with pytest.raises(FailedSpawnTerminalizationError, match="durable terminalization"):
            harness._process_queue_event()
        assert ledger.queue_dispatch(DISPATCH)["state"] == "queued"
        ledger.close()


def _private_capability_payload(
    root: Path,
    *,
    requirements: tuple[PluginRequirement, ...] = (),
    snapshots: tuple[PluginCapabilitySnapshot, ...] = (),
) -> dict[str, object]:
    return {
        "version": 1,
        "operation": "submit_result",
        "dispatch_id": DISPATCH,
        "generation": 1,
        "attempt": 1,
        "backend": "sdk_headless",
        "workspace_path": str(root),
        "schema_sha256": model_facing_result_schema_sha256(),
        "token": "a" * 64,
        "socket_path": str(root / "harness.sock"),
        "worker_role": "leaf",
        "allowed_operations": ["submit_result"],
        "leaf_worker_policy": {
            "agents.enabled": False,
            "features.multi_agent": False,
            "config_overrides": ["agents.enabled=false", "features.multi_agent=false"],
        },
        "effective_permission": None,
        "native_profile_sha256": None,
        "native_compatibility_sha256": None,
        "plugin_requirements": [item.to_json() for item in requirements],
        "plugin_capabilities": [{**item.to_json(), "capability_sha256": item.capability_digest} for item in snapshots],
    }


@pytest.mark.parametrize("mutation", ("requirement", "snapshot", "capability_digest", "source"))
def test_harness_rejects_plugin_authority_drift_before_popen(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        row = ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
        assert row is not None
        requirement = PluginRequirement("demo", "1", "bundled", "0" * 64, ("demo.skill",), ())
        snapshot = PluginCapabilitySnapshot(
            "demo", "1", "bundled", True, "0" * 64, ("demo.skill",), (), PluginReadiness.READY
        )
        capsule = json.loads(str(row["capsule_json"]))
        route = json.loads(str(row["route_json"]))
        capsule["plugin_requirements"] = [requirement.to_json()]
        route["plugin_requirements"] = [requirement.to_json()]
        route["plugin_capabilities"] = [{**snapshot.to_json(), "capability_sha256": snapshot.capability_digest}]
        if mutation == "requirement":
            route["plugin_requirements"] = []
        elif mutation == "snapshot":
            route["plugin_capabilities"] = []
        elif mutation == "capability_digest":
            route["plugin_capabilities"][0]["capability_sha256"] = "f" * 64
        else:
            route["plugin_capabilities"][0]["source"] = "marketplace"
        row["capsule_json"] = json.dumps(capsule, sort_keys=True)
        row["route_json"] = json.dumps(route, sort_keys=True)
        harness = _configured_harness(ledger, root, epoch)
        popen_calls = 0

        def unexpected_popen(*args: object, **kwargs: object) -> _LiveChild:
            nonlocal popen_calls
            popen_calls += 1
            return _LiveChild()

        monkeypatch.setattr(harness_module.subprocess, "Popen", unexpected_popen)
        with pytest.raises(HarnessError, match="plugin capability"):
            harness._prepare_and_spawn_one(row, recovery_continuation=False, created_paths=[])
        assert popen_calls == 0
        ledger.close()


def test_harness_fresh_plugin_drift_precedes_all_launch_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "codex-home"
        plugin = home / "plugins" / "cache" / "marketplace" / "demo" / "1.0"
        (plugin / ".codex-plugin").mkdir(parents=True)
        skill = plugin / "skills" / "demo.skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("verified\n", encoding="utf-8")
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            '{"name":"demo","version":"1.0","skills":"./skills/","enabled":true}\n', encoding="utf-8"
        )
        snapshot = discover_plugin_capabilities(home)[0]
        requirement = PluginRequirement(
            snapshot.canonical_id,
            snapshot.version,
            snapshot.source,
            snapshot.bundle_digest,
            ("demo.skill",),
            (),
        )
        ledger = _production_queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        row = ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
        assert row is not None
        capsule = json.loads(str(row["capsule_json"]))
        route = json.loads(str(row["route_json"]))
        capsule["plugin_requirements"] = [requirement.to_json()]
        route["plugin_requirements"] = [requirement.to_json()]
        route["plugin_capabilities"] = [{**snapshot.to_json(), "capability_sha256": snapshot.capability_digest}]
        row["capsule_json"] = json.dumps(capsule, sort_keys=True)
        row["route_json"] = json.dumps(route, sort_keys=True)
        harness = _configured_harness(ledger, root, epoch)
        queue_before = ledger.queue_dispatch(DISPATCH)
        recovery_before = ledger.recovery_state(DISPATCH)
        created_paths: list[Path] = []
        popen_calls = 0

        def unexpected_popen(*args: object, **kwargs: object) -> _LiveChild:
            nonlocal popen_calls
            popen_calls += 1
            return _LiveChild()

        monkeypatch.setenv("CODEX_HOME", str(home))
        monkeypatch.setattr(harness_module.subprocess, "Popen", unexpected_popen)
        (skill / "SKILL.md").write_text("drifted\n", encoding="utf-8")

        with pytest.raises(HarnessError, match="plugin capability changed"):
            harness._prepare_and_spawn_one(row, recovery_continuation=False, created_paths=created_paths)

        assert ledger.queue_dispatch(DISPATCH) == queue_before
        assert ledger.recovery_state(DISPATCH) == recovery_before
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert list(harness.runtime_root.iterdir()) == []
        assert created_paths == []
        assert popen_calls == 0
        ledger.close()


def test_worker_rejects_private_plugin_capability_drift_before_sdk_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        capability_path = root / "capability.json"
        capsule_path = root / "capsule.json"
        requirement = PluginRequirement("demo", "1", "bundled", "0" * 64, ("demo.skill",), ())
        worker_module.write_capability(
            capability_path,
            _private_capability_payload(root),
        )
        capsule_path.write_text(
            json.dumps(
                {
                    "model": "gpt-test",
                    "reasoning_effort": "medium",
                    "prompt": "bounded",
                    "workspace_path": str(root),
                    "plugin_requirements": [requirement.to_json()],
                }
            ),
            encoding="utf-8",
        )
        capsule_path.chmod(0o400)
        adapter_constructions = 0

        def unexpected_adapter(*args: object, **kwargs: object) -> object:
            nonlocal adapter_constructions
            adapter_constructions += 1
            return object()

        monkeypatch.setattr(worker_module, "CodexSdkAdapter", unexpected_adapter)
        with pytest.raises(WorkerError, match="plugin capability authority"):
            worker_module.run_sdk_worker(
                capability_file=capability_path,
                capsule_file=capsule_path,
                result_file=root / "result.json",
                socket_path=root / "harness.sock",
            )
        assert adapter_constructions == 0


@pytest.mark.parametrize("mutation", ("unsupported", "substitution"))
def test_result_submission_rejects_private_capability_shape_or_substitution_before_ipc(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        capability_path = root / "capability.json"
        result_path = root / "result.json"
        result_path.write_text(_completed_worker_result(), encoding="utf-8")
        requirement = PluginRequirement("demo", "1", "bundled", "0" * 64, ("demo.skill",), ())
        snapshot = PluginCapabilitySnapshot(
            "demo", "1", "bundled", True, "0" * 64, ("demo.skill",), (), PluginReadiness.READY
        )
        payload = _private_capability_payload(
            root,
            requirements=(requirement,) if mutation == "substitution" else (),
            snapshots=(snapshot,) if mutation == "substitution" else (),
        )
        worker_module.write_capability(capability_path, payload)
        sent = 0

        def unexpected_send(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal sent
            sent += 1
            return {}

        monkeypatch.setattr(worker_module, "send_request", unexpected_send)
        if mutation == "unsupported":
            capability_path.chmod(0o600)
            payload.pop("plugin_capabilities")
            capability_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            capability_path.chmod(0o400)
            with pytest.raises(WorkerError, match="unsupported closed shape"):
                worker_module.submit_result(capability_file=capability_path, result_file=result_path)
        else:
            expected = worker_module._read_private_capability(capability_path)
            capability_path.chmod(0o600)
            replacement_requirement = PluginRequirement(
                "replacement", "1", "bundled", "0" * 64, ("replacement.skill",), ()
            )
            replacement_snapshot = PluginCapabilitySnapshot(
                "replacement",
                "1",
                "bundled",
                True,
                "0" * 64,
                ("replacement.skill",),
                (),
                PluginReadiness.READY,
            )
            payload["plugin_requirements"] = [replacement_requirement.to_json()]
            payload["plugin_capabilities"] = [
                {**replacement_snapshot.to_json(), "capability_sha256": replacement_snapshot.capability_digest}
            ]
            capability_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            capability_path.chmod(0o400)
            with pytest.raises(WorkerError, match="changed before result submission"):
                worker_module._submit_result(
                    capability_file=capability_path,
                    result_file=result_path,
                    socket_path=None,
                    expected_capability=expected,
                )
        assert sent == 0


def test_harness_launched_detached_worker_submits_terminal_result_over_production_ipc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "codex-home"
        plugin = home / "plugins" / "cache" / "marketplace" / "demo" / "1.0"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / "skills" / "demo.skill").mkdir(parents=True)
        (plugin / "skills" / "demo.skill" / "SKILL.md").write_text("bounded\n", encoding="utf-8")
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            '{"name":"demo","version":"1.0","skills":"./skills/","enabled":true}\n', encoding="utf-8"
        )
        snapshot = discover_plugin_capabilities(home)[0]
        requirement = PluginRequirement(
            snapshot.canonical_id,
            snapshot.version,
            snapshot.source,
            snapshot.bundle_digest,
            ("demo.skill",),
            (),
        )
        ledger = _production_queued_ledger(root)
        queued = ledger.queue_dispatch(DISPATCH)
        capsule = json.loads(str(queued["capsule_json"]))
        route = json.loads(str(queued["route_json"]))
        capsule["plugin_requirements"] = [requirement.to_json()]
        route["plugin_requirements"] = [requirement.to_json()]
        route["plugin_capabilities"] = [{**snapshot.to_json(), "capability_sha256": snapshot.capability_digest}]
        ledger._db().execute(
            "UPDATE dispatch_queue SET capsule_json = ?, route_json = ? WHERE dispatch_id = ?",
            (json.dumps(capsule, sort_keys=True), json.dumps(route, sort_keys=True), DISPATCH),
        )
        ledger._db().commit()
        epoch = int(_harness(ledger, root)["epoch"])
        row = ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
        assert row is not None
        harness = _configured_harness(ledger, root, epoch)
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.bind(str(harness.socket_path))
        os.chmod(harness.socket_path, 0o600)
        endpoint.listen(1)
        endpoint.settimeout(5.0)
        harness._socket = endpoint
        terminal_result = _completed_worker_result()
        worker_script = (
            "import argparse; from pathlib import Path; from codex_flow.worker import submit_result; "
            "parser=argparse.ArgumentParser(); "
            "parser.add_argument('--capability-file', type=Path, required=True); "
            "parser.add_argument('--result-file', type=Path, required=True); "
            "parser.add_argument('--socket-path', type=Path, required=True); "
            "args,_=parser.parse_known_args(); "
            f"args.result_file.write_text({terminal_result!r}, encoding='utf-8'); "
            "submit_result(capability_file=args.capability_file, result_file=args.result_file, "
            "socket_path=args.socket_path)"
        )
        harness.worker_command = (sys.executable, "-c", worker_script)
        monkeypatch.setenv("CODEX_HOME", str(home))

        harness._prepare_and_spawn_one(row, recovery_continuation=False, created_paths=[])
        connection, _ = endpoint.accept()
        harness._accept_connection(connection)
        child = harness._children[DISPATCH]
        assert child.wait(timeout=5.0) == 0

        queue = ledger.queue_dispatch(DISPATCH)
        assert queue["state"] == "completed"
        assert queue["raw_result_json"] == terminal_result
        digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        capability_path = harness.runtime_root / f"capability-{digest}-g1-a1.json"
        capability = json.loads(capability_path.read_text(encoding="utf-8"))
        assert capability["plugin_requirements"] == [requirement.to_json()]
        assert capability["plugin_capabilities"] == [
            {**snapshot.to_json(), "capability_sha256": snapshot.capability_digest}
        ]
        harness.close()


def _harness(ledger: Ledger, root: Path, *, lease_seconds: float = 30.0) -> dict[str, object]:
    digest = hashlib.sha256(b"test-executable").hexdigest()
    nonce = hashlib.sha256(b"test-owner").hexdigest()
    return ledger.acquire_harness(
        repository_root=root,
        state_root=root,
        pid=os.getpid(),
        process_birth_identity="test-process",
        executable_digest=digest,
        version="test",
        owner_nonce_sha256=nonce,
        lease_seconds=lease_seconds,
    )


def test_wake_delivery_exhausts_bounded_retry_in_one_triggering_event(tmp_path: Path) -> None:
    ledger = _queued_ledger(tmp_path)
    authority = _harness(ledger, tmp_path)
    ledger._db().execute(
        "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?", (DISPATCH,)
    )
    ledger._db().commit()
    decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint", source_thread_id="source-thread")
    harness = _configured_harness(ledger, tmp_path, int(authority["epoch"]))
    calls: list[tuple[str, str]] = []

    def unavailable(source_thread_id: str, payload: str) -> str:
        calls.append((source_thread_id, payload))
        raise RuntimeError("source notification unavailable")

    harness._wake_delivery = unavailable

    assert harness._deliver_wakes()
    assert len(calls) == 2
    wake = ledger.wake_outbox(state="failed")
    assert len(wake) == 1 and wake[0]["attempt_count"] == 2
    assert ledger.wake_outbox(state="pending") == ()
    assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.AWAITING_CLAIM
    harness.close()


def _completed_worker_result() -> str:
    return json.dumps(
        {
            "changed_surfaces": [],
            "durable_status": "completed",
            "next_action": None,
            "schema_version": 1,
            "status": "completed",
            "summary": "bounded worker completed",
            "validations": [],
        },
        sort_keys=True,
    )


def test_terminal_result_preserves_contiguous_checkpoint_audit_with_middle_action() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute(
            "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        epoch = int(_harness(ledger, root)["epoch"])
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="checkpoint-result-worker",
            lease_token_sha256=token_sha256,
        )

        first = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:00Z")
        assert first is not None
        for _ in range(2):
            assert ledger.claim_wake(str(first["delivery_id"])) is not None
            ledger.record_wake_delivery(str(first["delivery_id"]), outcome="failed")

        ledger.rearm_checkpoint(DISPATCH, seconds=1)
        second = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:01Z")
        assert second is not None
        claimed_wake = ledger.claim_wake(str(second["delivery_id"]))
        assert claimed_wake is not None
        ledger.record_wake_delivery(
            str(second["delivery_id"]),
            outcome="delivered",
            source_turn_id="controller-turn",
        )
        decision = ledger.controller_decision(str(second["decision_id"]))
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=decision.revision,
        )
        bundle = ModelFacingControllerActionBundle(
            1,
            decision.decision_id,
            claim.generation,
            "middle-checkpoint-action",
            claim.revision,
            (),
            (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
            "retain this acknowledged audit fact",
        )
        receipt = ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
        ledger.acknowledge_controller_action(
            decision.decision_id,
            action_id=bundle.action_id,
            bundle_sha256=bundle.sha256,
            committed_revision=receipt.expected_revision + 1,
            claimant_id="operator",
            token=str(claim.token),
        )

        ledger.rearm_checkpoint(DISPATCH, seconds=1)
        third = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:02Z")
        assert third is not None

        # Reproduce the real recovery history: actionless controller
        # inspections can become ambiguous while a middle cycle commits an
        # action.  Terminal supersession must clear the attention-only reason
        # as well as preserving every cycle.
        attention_at = "9999-01-01T00:00:03Z"
        for status in (first, third):
            ledger._db().execute(
                "UPDATE wake_outbox SET state = 'failed', attempt_count = 2, updated_at = ? WHERE decision_id = ?",
                (attention_at, str(status["decision_id"])),
            )
            ledger._db().execute(
                "UPDATE controller_decision_generations SET state = 'ambiguous', "
                "inspection_started_at = ?, inspection_completed_at = ?, inspection_outcome = 'ambiguous', "
                "terminal_at = ?, updated_at = ? WHERE decision_id = ? AND generation = 1",
                (attention_at, attention_at, attention_at, attention_at, str(status["decision_id"])),
            )
            ledger._db().execute(
                "UPDATE controller_decisions SET state = 'human_attention_required', "
                "human_attention_reason = 'controller inspection: ambiguous', updated_at = ? "
                "WHERE decision_id = ?",
                (attention_at, str(status["decision_id"])),
            )
        ledger._db().commit()

        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )

        assert terminal["state"] == "completed"
        checkpoints = [
            item for item in ledger.wake_outbox() if item["dispatch_id"] == DISPATCH and item["kind"] == "checkpoint"
        ]
        assert [int(item["cycle_sequence"]) for item in checkpoints] == [1, 2, 3]
        assert [item["state"] for item in checkpoints] == ["suppressed", "delivered", "suppressed"]
        assert ledger.controller_decision(str(first["decision_id"])).state is ControllerDecisionState.SUPERSEDED
        assert ledger.controller_decision(str(second["decision_id"])).state is ControllerDecisionState.ACKNOWLEDGED
        assert ledger.controller_decision(str(third["decision_id"])).state is ControllerDecisionState.SUPERSEDED
        remaining_attention_reasons = (
            ledger._db()
            .execute(
                "SELECT COUNT(*) FROM controller_decisions WHERE decision_id IN (?, ?) "
                "AND human_attention_reason IS NOT NULL",
                (str(first["decision_id"]), str(third["decision_id"])),
            )
            .fetchone()[0]
        )
        assert remaining_attention_reasons == 0
        assert [item["kind"] for item in ledger.wake_outbox(state="pending")] == ["terminal"]
        ledger.close()

        reopened = Ledger(root / "workflow.db")
        assert reopened.queue_dispatch(DISPATCH)["state"] == "completed"
        reopened.close()


def _prepare_result_transport_attention(
    ledger: Ledger,
    root: Path,
    epoch: int,
    *,
    token: str = "a" * 64,
) -> None:
    """Retain one exact exited attempt whose result transport was exhausted."""

    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
    ledger.issue_attempt_capability(
        DISPATCH,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=root,
        backend="sdk_headless",
        token_sha256=token_sha256,
        expires_at="9999-12-31T23:59:59Z",
    )
    ledger.bind_worker_liveness(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        pid=os.getpid(),
        process_birth_identity="result-transport-worker",
        lease_token_sha256=token_sha256,
    )
    ledger.mark_worker_exit(
        DISPATCH,
        pid=os.getpid(),
        process_birth_identity="result-transport-worker",
        exit_code=WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY,
        classification="result-transport-after-identity",
    )


def test_delayed_exact_result_is_accepted_after_result_transport_exit() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        _prepare_result_transport_attention(ledger, root, epoch)

        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token="a" * 64,
            raw_result=_completed_worker_result(),
        )

        assert terminal["state"] == "completed"
        capability = ledger.worker_attempt_capability(DISPATCH, generation=1, attempt=1, token="a" * 64)
        assert capability["consumed_at"] is not None
        assert capability["accepted_result_sha256"] == hashlib.sha256(_completed_worker_result().encode()).hexdigest()
        ledger.close()


@pytest.mark.parametrize(
    "guard", ["wrong-token", "active-worker", "unrelated-attention", "replaced-attempt", "pending-cancel"]
)
def test_retained_result_recovery_rejects_non_exact_owner(guard: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        _prepare_result_transport_attention(ledger, root, epoch)
        token = "a" * 64
        attempt = 1
        if guard == "wrong-token":
            token = "b" * 64
        elif guard == "active-worker":
            ledger._db().execute("UPDATE worker_liveness SET exited_at = NULL WHERE dispatch_id = ?", (DISPATCH,))
        elif guard == "unrelated-attention":
            ledger._db().execute(
                "UPDATE retry_policies SET last_failure = 'unknown' WHERE dispatch_id = ?", (DISPATCH,)
            )
        elif guard == "replaced-attempt":
            attempt = 2
        else:
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="cancel-before-retained-result",
                expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
                action_kind=RecoveryActionKind.CANCEL,
                reason="cancellation retains terminal ownership",
            )
        ledger._db().commit()

        with pytest.raises(StaleWriter):
            ledger.commit_retained_queue_result(
                DISPATCH,
                generation=1,
                attempt=attempt,
                token=token,
                raw_result=_completed_worker_result(),
            )

        assert ledger.queue_dispatch(DISPATCH)["raw_result_json"] is None
        ledger.close()


def test_restart_recovers_expired_private_result_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        epoch = int(_harness(ledger, root)["epoch"])
        _prepare_result_transport_attention(ledger, root, epoch)
        ledger._db().execute(
            "UPDATE attempt_capabilities SET expires_at = '2000-01-01T00:00:00Z' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        harness = _configured_harness(ledger, root, epoch)
        digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        capability_path = harness.runtime_root / f"capability-{digest}-g1-a1.json"
        result_path = harness.runtime_root / f"result-{digest}-g1-a1.json"
        worker_module.write_capability(capability_path, _private_capability_payload(root))
        result_path.write_text(_completed_worker_result(), encoding="utf-8")
        result_path.chmod(0o400)

        harness.recover_once()
        first = ledger.queue_dispatch(DISPATCH)
        harness.recover_once()

        assert first["state"] == "completed"
        assert ledger.queue_dispatch(DISPATCH) == first
        capability = ledger.worker_attempt_capability(DISPATCH, generation=1, attempt=1, token="a" * 64)
        assert capability["consumed_at"] is not None
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 1
        assert ledger._db().execute("SELECT COUNT(*) FROM wake_outbox").fetchone()[0] == 1
        ledger.close()


def test_recovered_attempt_repairs_past_capability_and_ignores_queue_deadline() -> None:
    """A stale checkpoint horizon must not expire the active recovered attempt."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute(
            "UPDATE dispatch_queue SET deadline = '2000-01-01T00:00:00Z' WHERE dispatch_id = ?", (DISPATCH,)
        )
        ledger._db().commit()
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])

        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        recovered = ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        assert recovered["attempt"] == 2
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)

        token = "recovered-worker-token"
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        capability = ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=2,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
            expires_at="2000-01-01T00:00:00Z",
            lease_seconds=60,
        )
        assert str(capability["expires_at"]) > "2000-01-01T00:00:00Z"
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=2,
            pid=os.getpid(),
            process_birth_identity="recovered-worker",
            lease_token_sha256=token_hash,
            lease_seconds=60,
        )

        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=2,
            token=token,
            raw_result=_completed_worker_result(),
        )
        assert terminal["state"] == "completed"
        assert ledger.queue_dispatch(DISPATCH)["state"] == "completed"
        assert ledger.queue_dispatch(DISPATCH)["deadline"] == "2000-01-01T00:00:00Z"
        ledger.close()


def test_worker_heartbeat_renews_the_exact_attempt_capability() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "heartbeat-worker-token"
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        issued = ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
            expires_at=None,
            lease_seconds=1,
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="heartbeat-worker",
            lease_token_sha256=token_hash,
            lease_seconds=1,
        )
        before = ledger.worker_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
        )
        assert str(before["expires_at"]) >= str(issued["expires_at"])
        renewed = ledger.renew_worker_liveness(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            lease_seconds=60,
        )
        after = ledger.worker_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
        )
        assert str(after["expires_at"]) > str(before["expires_at"])
        assert renewed["last_seen_at"] == ledger.worker_liveness(DISPATCH)["last_seen_at"]  # type: ignore[index]
        ledger.close()


def test_replaced_attempts_and_wrong_tokens_remain_rejected() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        old_token = "old-worker-token"
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(old_token.encode("ascii")).hexdigest(),
            expires_at=None,
        )
        ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)

        with pytest.raises(StaleWriter, match="capability does not exist"):
            ledger.commit_queue_result(
                DISPATCH,
                generation=1,
                attempt=1,
                token=old_token,
                raw_result=_completed_worker_result(),
            )

        new_token = "new-worker-token"
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=2,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(new_token.encode("ascii")).hexdigest(),
            expires_at=None,
        )
        with pytest.raises(StaleWriter, match="token is invalid"):
            ledger.commit_queue_result(
                DISPATCH,
                generation=1,
                attempt=2,
                token="wrong-token",
                raw_result=_completed_worker_result(),
            )
        ledger.close()


def test_worker_result_boundary_distinguishes_malformed_output_and_acknowledges_valid_result() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "boundary-worker-token"
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=None,
        )
        harness = _configured_harness(ledger, root, epoch)
        malformed_payload = {
            "version": 1,
            "operation": "submit_result",
            "dispatch_id": DISPATCH,
            "generation": 1,
            "attempt": 1,
            "backend": "sdk_headless",
            "workspace_path": str(root),
            "schema_sha256": model_facing_result_schema_sha256(),
            "token": token,
            "raw_result": "not-json",
        }
        with pytest.raises(WorkerResultRejected) as rejected:
            harness._submit_result(malformed_payload)
        assert rejected.value.code is WorkerResultRejectionCode.MALFORMED_OUTPUT
        assert ledger.recent_activity(DISPATCH)[-1]["text"] == "malformed_model_output"
        assert "not-json" not in json.dumps(ledger.recent_activity(DISPATCH))

        left, right = socket.socketpair()
        try:
            left.sendall(encode_frame(malformed_payload))
            harness._accept_connection(right)
            response = decode_frame(left)
        finally:
            left.close()
        assert response["ok"] is False
        assert response["reason_code"] == "malformed_model_output"

        ledger._db().execute(
            "UPDATE attempt_capabilities SET expires_at = '2000-01-01T00:00:00Z' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        valid_payload = {**malformed_payload, "raw_result": _completed_worker_result()}
        left, right = socket.socketpair()
        try:
            left.sendall(encode_frame(valid_payload))
            harness._accept_connection(right)
            response = decode_frame(left)
        finally:
            left.close()
        assert response["ok"] is False
        assert response["reason_code"] == "result_capability_expired"

        # Re-issuing unchanged active-attempt facts repairs the expired
        # capability before the valid terminal envelope is accepted.
        harness.ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=None,
        )

        response = harness._submit_result(valid_payload)
        assert response["ok"] is True
        assert response["terminal_status"] == "completed"
        assert ledger.queue_dispatch(DISPATCH)["state"] == "completed"
        harness.close()
        ledger.close()


def test_worker_result_submit_retries_eagain_with_same_request_and_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        capability_path = root / "capability.json"
        result_path = root / "result.json"
        result = _completed_worker_result()
        result_path.write_text(result, encoding="utf-8")
        worker_module.write_capability(capability_path, _private_capability_payload(root))
        requests: list[dict[str, object]] = []
        sleeps: list[float] = []

        def flaky_send(_endpoint: Path, request: dict[str, object], **_: object) -> dict[str, object]:
            requests.append(request)
            if len(requests) == 1:
                raise BlockingIOError(errno.EAGAIN, "would block")
            return {"version": 1, "ok": True, "dispatch_id": DISPATCH, "terminal_status": "completed"}

        monkeypatch.setattr(worker_module, "send_request", flaky_send)
        monkeypatch.setattr(worker_module.time, "sleep", sleeps.append)

        response = worker_module.submit_result(capability_file=capability_path, result_file=result_path)

        assert response["ok"] is True
        assert len(requests) == 2
        assert requests[0] == requests[1]
        assert requests[0]["raw_result"] == result
        assert sleeps == [worker_module.RESULT_SUBMIT_BACKOFF_SECONDS]


def test_worker_result_submit_replays_after_lost_ack_without_duplicate_terminal_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "a" * 64
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=None,
        )
        harness = _configured_harness(ledger, root, epoch)
        capability_path = root / "capability.json"
        result_path = root / "result.json"
        result_path.write_text(_completed_worker_result(), encoding="utf-8")
        worker_module.write_capability(capability_path, _private_capability_payload(root))
        calls = 0

        def commit_then_lose_ack(_endpoint: Path, request: dict[str, object], **_: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            response = harness._submit_result(request)
            if calls == 1:
                raise IpcTransportError("ack lost after durable commit")
            return response

        monkeypatch.setattr(worker_module, "send_request", commit_then_lose_ack)
        response = worker_module.submit_result(capability_file=capability_path, result_file=result_path)

        assert response["ok"] is True
        assert calls == 2
        assert ledger.queue_dispatch(DISPATCH)["state"] == "completed"
        assert len(ledger.wake_outbox()) == 1
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 1
        assert ledger._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
        assert (
            ledger.queue_dispatch(DISPATCH)["raw_result_sha256"]
            == hashlib.sha256(_completed_worker_result().encode("utf-8")).hexdigest()
        )
        harness.close()
        ledger.close()


def test_worker_result_submit_exhaustion_is_distinct_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        capability_path = root / "capability.json"
        result_path = root / "result.json"
        result_path.write_text(_completed_worker_result(), encoding="utf-8")
        worker_module.write_capability(capability_path, _private_capability_payload(root))
        calls = 0

        def always_block(_endpoint: Path, _request: dict[str, object], **_: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise BlockingIOError(errno.EAGAIN, "would block")

        monkeypatch.setattr(worker_module, "send_request", always_block)
        monkeypatch.setattr(worker_module.time, "sleep", lambda _delay: None)

        with pytest.raises(ResultTransportAfterIdentity, match="transport recovery exhausted"):
            worker_module.submit_result(capability_file=capability_path, result_file=result_path)
        assert calls == worker_module.RESULT_SUBMIT_MAX_ATTEMPTS
        assert result_path.read_text(encoding="utf-8") == _completed_worker_result()


@pytest.mark.parametrize(
    "response",
    [
        {"version": 1, "ok": False, "reason_code": WorkerResultRejectionCode.CAPABILITY_STALE.value},
        {"version": 2, "ok": True, "dispatch_id": DISPATCH},
    ],
)
def test_worker_result_submit_does_not_retry_rejection_or_invalid_ack(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        capability_path = root / "capability.json"
        result_path = root / "result.json"
        result_path.write_text(_completed_worker_result(), encoding="utf-8")
        worker_module.write_capability(capability_path, _private_capability_payload(root))
        calls = 0

        def rejected(_endpoint: Path, _request: dict[str, object], **_: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return response

        monkeypatch.setattr(worker_module, "send_request", rejected)
        with pytest.raises(WorkerError):
            worker_module.submit_result(capability_file=capability_path, result_file=result_path)
        assert calls == 1


def test_harness_classifies_exhausted_result_transport_without_malformed_retry() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
            expires_at=None,
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="result-transport-worker",
            lease_token_sha256=token_hash,
        )
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY)}

        assert harness._reap_children() is True

        queue = ledger.queue_dispatch(DISPATCH)
        retry = ledger.retry_policy(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        assert queue["state"] == "human_attention_required"
        assert retry["last_failure"] == "result_transport_after_identity"
        assert retry["strategy"] == "human_attention_required"
        assert retry["human_attention_reason"] == "result submission transport recovery exhausted after worker identity"
        assert retry["pre_identity_used"] == 0
        assert recovery["human_attention_reason"] == retry["human_attention_reason"]
        assert queue["raw_result_json"] is None
        harness.close()
        ledger.close()


def test_terminal_result_preserves_historical_human_attention_action_and_wake() -> None:
    """Closing a dispatch must not rewrite an already-audited controller action."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=decision.revision,
        )
        bundle = ModelFacingControllerActionBundle(
            1,
            decision.decision_id,
            claim.generation,
            "historical-human-attention",
            claim.revision,
            (),
            (ModelFacingControllerAction(ControllerActionKind.REQUIRE_HUMAN_ATTENTION, reason="follow up"),),
            "retain the historical action audit",
        )
        receipt = ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
        ledger.acknowledge_controller_action(
            decision.decision_id,
            action_id=receipt.action_id,
            bundle_sha256=bundle.sha256,
            committed_revision=receipt.expected_revision + 1,
            claimant_id="operator",
            token=str(claim.token),
        )
        historical = ledger.controller_decision(decision.decision_id)
        assert historical.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
        assert historical.action_id == "historical-human-attention"

        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "terminal-worker-token"
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=None,
        )
        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )
        assert terminal["state"] == "completed"

        preserved = ledger.controller_decision(decision.decision_id)
        assert preserved.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
        assert preserved.action_id == "historical-human-attention"
        historical_wakes = [item for item in ledger.wake_outbox() if item["decision_id"] == decision.decision_id]
        assert len(historical_wakes) == 1
        assert historical_wakes[0]["state"] not in {"pending", "starting"}
        ledger.close()

        reopened = Ledger(root / "workflow.db")
        assert reopened.controller_decision(decision.decision_id).action_id == "historical-human-attention"
        reopened.close()


def test_resultless_cancellation_is_terminal_and_cannot_restart() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        cancelled = ledger.cancel_queue_dispatch(DISPATCH)

        assert cancelled["state"] == "cancelled"
        assert cancelled["raw_result_json"] is None
        assert cancelled["raw_result_sha256"] is None
        assert cancelled["terminal_status"] is None
        assert cancelled["claim_epoch"] is None
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 0
        assert ledger._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0

        assert ledger.cancel_queue_dispatch(DISPATCH) == cancelled
        assert ledger.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="a" * 64) is None
        assert ledger.recover_queue_dispatch(DISPATCH, new_epoch=int(authority["epoch"])) == cancelled
        assert ledger.finalize_queue_result(DISPATCH) == cancelled
        with pytest.raises(StaleWriter, match="terminal dispatch"):
            ledger.record_notification_attempt(
                DISPATCH,
                source_task=None,
                payload_digest=hashlib.sha256(b"cancelled").hexdigest(),
                outcome="attempted_ok",
            )
        ledger.close()

        reopened = Ledger(root / "workflow.db")
        assert reopened.queue_dispatch(DISPATCH)["state"] == "cancelled"
        assert reopened.claim_queue_dispatch(epoch=int(authority["epoch"]), claim_nonce_sha256="b" * 64) is None
        reopened.close()


def test_resultless_cancellation_supersedes_failed_checkpoint_wake() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute(
            "UPDATE queue_bindings SET source_thread_id = 'source-thread' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="checkpoint-worker",
            lease_token_sha256=token_sha256,
        )
        checkpoint = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:00Z")
        assert checkpoint is not None
        decision_id = str(checkpoint["decision_id"])
        delivery_id = str(checkpoint["delivery_id"])
        for _ in range(2):
            assert ledger.claim_wake(delivery_id) is not None
            ledger.record_wake_delivery(delivery_id, outcome="failed")
        ledger.mark_worker_exit(
            DISPATCH,
            pid=os.getpid(),
            process_birth_identity="checkpoint-worker",
        )

        cancelled = ledger.cancel_queue_dispatch(DISPATCH)

        assert cancelled["state"] == "cancelled"
        assert not [item for item in ledger.wake_outbox() if item["dispatch_id"] == DISPATCH]
        decision = ledger.controller_decision(decision_id)
        assert decision.state is ControllerDecisionState.SUPERSEDED
        assert (
            ledger._db()
            .execute("SELECT human_attention_reason FROM controller_decisions WHERE decision_id = ?", (decision_id,))
            .fetchone()[0]
            is None
        )
        ledger.close()


def test_running_reasoned_cancellation_waits_for_exact_child_exit_acknowledgement() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="owned-worker",
            lease_token_sha256=token_sha256,
        )
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        action = ledger.apply_recovery_action(
            DISPATCH,
            action_id="cancel-owned-worker",
            expected_revision=revision,
            action_kind=RecoveryActionKind.CANCEL,
            reason="stop bounded work",
        )

        assert ledger.queue_dispatch(DISPATCH)["state"] == "starting"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is None  # type: ignore[index]
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 1
        assert ledger.retry_policy(DISPATCH)["revision"] == revision + 1
        with pytest.raises(StaleWriter, match="cancellation is already pending"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="second-action-after-cancel",
                expected_revision=revision + 1,
                action_kind=RecoveryActionKind.BUDGET_CHANGE,
                reason="must not overtake stop ownership",
                requested_budget=RetryBudgetChange(5, 1, 2, 1),
            )

        child = _StoppingChild()
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: child}  # type: ignore[assignment]
        assert harness._advance_worker_cancellation(action) is True

        assert child.terminated is True
        assert ledger.queue_dispatch(DISPATCH)["state"] == "cancelled"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is not None  # type: ignore[index]
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        assert harness._children == {}
        ledger.close()


def test_human_attention_reasoned_cancellation_uses_the_same_stop_protocol() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="attention-worker",
            lease_token_sha256=token_sha256,
        )
        ledger.mark_human_attention_required(DISPATCH, reason="operator attention")
        assert ledger.queue_binding(DISPATCH)["checkpoint_armed"] == 0
        assert ledger.next_checkpoint_deadline() is None
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        action = ledger.apply_recovery_action(
            DISPATCH,
            action_id="cancel-attention-worker",
            expected_revision=revision,
            action_kind=RecoveryActionKind.CANCEL,
            reason="operator requested stop",
        )
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"

        child = _StoppingChild()
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: child}  # type: ignore[assignment]
        assert harness._advance_worker_cancellation(action) is True
        assert ledger.queue_dispatch(DISPATCH)["state"] == "cancelled"
        ledger.close()


def test_recovery_inspection_human_attention_disarms_checkpoint_deadline() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        _prepare_idle_continuation(ledger, root, epoch)

        ledger.record_recovery_inspection(
            DISPATCH,
            kind="malformed",
            thread_id="old-thread",
            attention_reason="provider rejected the structured output schema",
        )

        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_binding(DISPATCH)["checkpoint_armed"] == 0
        assert ledger.next_checkpoint_deadline() is None
        ledger._db().execute(
            "UPDATE queue_bindings SET checkpoint_deadline = '2000-01-01T00:00:00Z', checkpoint_armed = 1 "
            "WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        assert ledger.next_checkpoint_deadline() == "2000-01-01T00:00:00Z"
        checkpoint = ledger.claim_due_checkpoint(epoch=epoch, now="2000-01-01T00:00:01Z")
        assert checkpoint is not None
        decision = ledger.controller_decision(str(checkpoint["decision_id"]))
        assert decision.summary.dispatch_state == "human_attention_required"
        assert decision.summary.retry_policy_revision == int(ledger.retry_policy(DISPATCH)["revision"])
        assert decision.summary.human_attention_reason == "provider rejected the structured output schema"
        assert decision.summary.worker_exit_classification == "worker-exit"
        assert decision.summary.worker_exit_code is None
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        ledger.apply_recovery_action(
            DISPATCH,
            action_id="retry-after-schema-repair",
            expected_revision=revision,
            action_kind=RecoveryActionKind.RETRY,
            reason="provider schema compatibility repaired",
        )
        assert ledger.queue_binding(DISPATCH)["checkpoint_armed"] == 0
        ledger.close()


def test_cancellation_acknowledgement_failure_retains_child_ownership_for_reap_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="retry-cancel-worker",
            lease_token_sha256=token_sha256,
        )
        action = ledger.apply_recovery_action(
            DISPATCH,
            action_id="cancel-with-ack-retry",
            expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
            action_kind=RecoveryActionKind.CANCEL,
            reason="stop and retain ownership",
        )
        child = _StoppingChild()
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: child}  # type: ignore[assignment]
        original = ledger.acknowledge_worker_cancellation_exit
        calls = 0

        def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LedgerError("injected cancellation acknowledgement failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(ledger, "acknowledge_worker_cancellation_exit", fail_once)
        with pytest.raises(LedgerError, match="acknowledgement failure"):
            harness._advance_worker_cancellation(action)
        assert harness._children == {DISPATCH: child}
        assert ledger.queue_dispatch(DISPATCH)["state"] == "starting"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is None  # type: ignore[index]

        assert harness._reap_children() is True
        assert calls == 2
        assert harness._children == {}
        assert ledger.queue_dispatch(DISPATCH)["state"] == "cancelled"
        ledger.close()


@pytest.mark.parametrize("resolution", ["reap", "restart"])
def test_cancel_before_result_rejects_result_and_resolves_one_terminal_owner(resolution: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        worker_pid = os.getpid() if resolution == "reap" else 2_000_000_000
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=worker_pid,
            process_birth_identity=f"{resolution}-cancelled-worker",
            lease_token_sha256=token_sha256,
        )
        ledger.apply_recovery_action(
            DISPATCH,
            action_id=f"{resolution}-cancel-before-result",
            expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
            action_kind=RecoveryActionKind.CANCEL,
            reason="cancellation owns terminal authority",
        )

        with pytest.raises(StaleWriter, match="pending worker cancellation"):
            ledger.commit_queue_result(
                DISPATCH,
                generation=1,
                attempt=1,
                token=token,
                raw_result=_completed_worker_result(),
            )
        assert ledger.queue_dispatch(DISPATCH)["state"] == "starting"
        capability = ledger.worker_attempt_capability(DISPATCH, generation=1, attempt=1, token=token)
        assert capability["consumed_at"] is None

        harness = _configured_harness(ledger, root, epoch)
        if resolution == "reap":
            harness._children = {DISPATCH: _ExitedChild(-15)}  # type: ignore[assignment]
            assert harness._reap_children() is True
            assert harness._reap_children() is False
        else:
            assert harness._advance_pending_cancellations() is True
            assert harness._advance_pending_cancellations() is False

        queue = ledger.queue_dispatch(DISPATCH)
        live = ledger.worker_liveness(DISPATCH)
        assert queue["state"] == "cancelled"
        assert queue["raw_result_json"] is None
        assert live is not None and live["exited_at"] is not None
        assert ledger.pending_cancellation_actions() == ()
        assert harness._children == {}
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 0
        ledger.close()


def test_result_before_cancel_remains_the_only_terminal_owner_after_reap() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="result-owning-worker",
            lease_token_sha256=token_sha256,
        )

        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )
        with pytest.raises(StaleWriter, match="terminal queue dispatch"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="cancel-after-result",
                expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
                action_kind=RecoveryActionKind.CANCEL,
                reason="late cancellation cannot replace a result",
            )

        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: _ExitedChild(0)}  # type: ignore[assignment]
        assert harness._reap_children() is True
        assert harness._reap_children() is False
        assert terminal["state"] == "completed"
        assert ledger.queue_dispatch(DISPATCH)["state"] == "completed"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is not None  # type: ignore[index]
        assert ledger.pending_cancellation_actions() == ()
        assert harness._children == {}
        ledger.close()


def test_cancelled_shape_rejects_partial_terminal_facts() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger.cancel_queue_dispatch(DISPATCH)
        ledger._db().execute(
            "UPDATE dispatch_queue SET terminal_status = 'completed' WHERE dispatch_id = ?", (DISPATCH,)
        )
        ledger._db().commit()
        ledger.close()
        with pytest.raises(CorruptSchemaError, match="fabricated result fact"):
            Ledger(root / "workflow.db")


def test_cancelled_queue_is_not_claimable_by_harness_process() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        ledger.cancel_queue_dispatch(DISPATCH)
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = int(authority["epoch"])
        harness._children = {}

        assert harness.process_once() is False
        assert harness._children == {}
        assert ledger.queue_dispatch(DISPATCH)["state"] == "cancelled"
        ledger.close()


def test_migrated_active_queue_rebinds_route_and_binding_atomically() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute("UPDATE queue_bindings SET plan_path = 'legacy-v10' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        authority = NativePermissionAuthority(
            NativePermissionMode.INHERIT_NATIVE,
            "danger-full-access",
            "never",
        )

        rebound = ledger.rebind_legacy_active_queue(
            DISPATCH,
            source_thread_id="source-thread",
            permission_mode=NativePermissionMode.INHERIT_NATIVE,
            native_profile_sha256="a" * 64,
            native_compatibility_sha256="b" * 64,
            effective_permission=authority,
        )

        assert rebound["source_thread_id"] == "source-thread"
        assert rebound["permission_mode"] == "inherit_native"
        assert rebound["checkpoint_armed"] == 1
        route = (
            ledger._db()
            .execute("SELECT route_json FROM dispatch_queue WHERE dispatch_id = ?", (DISPATCH,))
            .fetchone()[0]
        )
        assert '"sandbox_mode":"danger-full-access"' in route
        assert '"native_profile_sha256":"' + "a" * 64 + '"' in route
        assert (
            ledger.rebind_legacy_active_queue(
                DISPATCH,
                source_thread_id=ThreadIdentity("source-thread"),
                permission_mode=NativePermissionMode.INHERIT_NATIVE,
                native_profile_sha256="a" * 64,
                native_compatibility_sha256="b" * 64,
                effective_permission=authority,
            )["updated_at"]
            == rebound["updated_at"]
        )
        upgraded = ledger.rebind_legacy_active_queue(
            DISPATCH,
            source_thread_id="source-thread",
            permission_mode=NativePermissionMode.INHERIT_NATIVE,
            native_profile_sha256="a" * 64,
            native_compatibility_sha256="c" * 64,
            effective_permission=authority,
        )
        assert upgraded["native_compatibility_sha256"] == "c" * 64
        upgraded_route = (
            ledger._db()
            .execute("SELECT route_json FROM dispatch_queue WHERE dispatch_id = ?", (DISPATCH,))
            .fetchone()[0]
        )
        assert '"native_compatibility_sha256":"' + "c" * 64 + '"' in upgraded_route
        ledger.close()


def test_invalid_resumed_response_chain_enters_inspect_before_mutate_recovery() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="chain-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = epoch
        harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_RESPONSE_CHAIN_INVALID)}
        harness._resumed_children = {DISPATCH}

        assert harness._reap_children() is True
        recovered = ledger.queue_dispatch(DISPATCH)
        assert recovered["state"] == "recovery_inspection_pending"
        assert recovered["attempt"] == 1
        assert recovered["thread_id"] == "old-thread"
        assert ledger.recovery_state(DISPATCH)["inspection_used"] == 1
        ledger.close()


@pytest.mark.parametrize(
    ("inspection_kind", "turn_id", "durable_kind"),
    [
        (ThreadInspectionKind.EMPTY_HISTORY, None, "invalid_chain_empty_history"),
        (ThreadInspectionKind.FAILED_TURN, "failed-turn", "invalid_chain_failed_turn"),
    ],
)
def test_typed_invalid_chain_history_uses_the_single_fresh_thread_budget(
    inspection_kind: ThreadInspectionKind,
    turn_id: str | None,
    durable_kind: str,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="invalid-chain-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        ledger.mark_worker_exit(
            DISPATCH,
            pid=os.getpid(),
            process_birth_identity="invalid-chain-worker",
            classification="response-chain-invalid",
        )
        harness = _configured_harness(ledger, root, epoch)
        harness._thread_inspector = lambda thread_id: ThreadInspection(thread_id, inspection_kind, turn_id=turn_id)

        assert harness._recover_exited_dispatch(ledger.queue_dispatch(DISPATCH)) is True
        recovery = ledger.recovery_state(DISPATCH)
        assert recovery["recovery_state"] == "recovery_continuation_pending"
        assert recovery["inspected_kind"] == durable_kind
        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="9999-01-01T00:00:00Z",
        )
        assert claimed["thread_id"] is None
        assert ledger.retry_policy(DISPATCH)["invalid_chain_used"] == 1
        assert ledger.recovery_state(DISPATCH)["fresh_thread_used"] == 1
        ledger.close()


def test_non_chain_worker_exit_never_requeues_automatically() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = epoch
        harness._children = {DISPATCH: _ExitedChild(2)}
        harness._resumed_children = set()

        assert harness._reap_children() is True
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_dispatch(DISPATCH)["attempt"] == 1
        harness.recover_once()
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        ledger.close()


def test_transient_failed_turn_resumes_the_same_persisted_thread_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="transient-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute(
            "UPDATE dispatch_queue SET thread_id = 'thread-with-502' WHERE dispatch_id = ?", (DISPATCH,)
        )
        ledger._db().commit()
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_TRANSIENT_AFTER_IDENTITY)}  # type: ignore[assignment]
        harness._thread_inspector = lambda thread_id: ThreadInspection(
            thread_id,
            ThreadInspectionKind.FAILED_TURN,
            turn_id="failed-turn",
            retry_at="9999-01-01T00:00:00Z",
        )

        assert harness._reap_children() is True
        assert ledger.retry_policy(DISPATCH)["strategy"] == "same_thread_continuation"
        assert harness._recover_exited_dispatch(ledger.queue_dispatch(DISPATCH)) is True
        recovery = ledger.recovery_state(DISPATCH)
        assert recovery["recovery_state"] == "recovery_continuation_pending"
        assert recovery["inspected_kind"] == "transient_failed_turn"
        assert recovery["next_eligible_at"] == "9999-01-01T00:00:00.000000Z"

        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="9999-01-01T00:00:00Z",
        )
        assert claimed["attempt"] == 2
        assert claimed["thread_id"] == "thread-with-502"
        ledger.close()


def test_provider_transient_recovery_has_three_increasing_same_thread_continuations() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        harness = _configured_harness(ledger, root, epoch)
        harness._thread_inspector = lambda thread_id: ThreadInspection(
            thread_id,
            ThreadInspectionKind.FAILED_TURN,
            turn_id=f"failed-{thread_id}",
        )
        deadlines: list[str] = []

        for attempt in range(1, 4):
            if attempt == 1:
                ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
                ledger._db().execute(
                    "UPDATE dispatch_queue SET thread_id = 'provider-thread' WHERE dispatch_id = ?", (DISPATCH,)
                )
                ledger._db().commit()
            else:
                claimed = ledger.begin_recovery_continuation(
                    DISPATCH,
                    epoch=epoch,
                    claim_nonce_sha256=chr(96 + attempt) * 64,
                    now="9999-01-01T00:00:00Z",
                )
                assert claimed["thread_id"] == "provider-thread"
            ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
            token_sha256 = hashlib.sha256(f"provider-worker-{attempt}".encode("ascii")).hexdigest()
            ledger.issue_attempt_capability(
                DISPATCH,
                generation=1,
                attempt=attempt,
                operation="submit_result",
                schema_sha256=model_facing_result_schema_sha256(),
                workspace_path=root,
                backend="sdk_headless",
                token_sha256=token_sha256,
                expires_at="9999-12-31T23:59:59Z",
            )
            ledger.bind_worker_liveness(
                DISPATCH,
                epoch=epoch,
                generation=1,
                attempt=attempt,
                pid=os.getpid(),
                process_birth_identity=f"provider-worker-{attempt}",
                lease_token_sha256=token_sha256,
            )
            harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_TRANSIENT_AFTER_IDENTITY)}  # type: ignore[assignment]
            harness._resumed_children = {DISPATCH}

            assert harness._reap_children() is True
            policy = ledger.retry_policy(DISPATCH)
            assert policy["provider_transient_used"] == attempt
            assert policy["strategy"] == "same_thread_continuation"
            assert isinstance(policy["next_eligible_at"], str)
            deadlines.append(str(policy["next_eligible_at"]))

            assert harness._recover_exited_dispatch(ledger.queue_dispatch(DISPATCH)) is True
            recovery = ledger.recovery_state(DISPATCH)
            assert recovery["recovery_state"] == "recovery_continuation_pending"
            assert recovery["next_eligible_at"] == policy["next_eligible_at"]

        assert deadlines == sorted(deadlines)
        assert len(set(deadlines)) == 3
        final_claim = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="d" * 64,
            now="9999-01-01T00:00:00Z",
        )
        assert final_claim["attempt"] == 4
        assert final_claim["thread_id"] == "provider-thread"
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 3

        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        fourth_token_sha256 = hashlib.sha256(b"provider-worker-4").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=4,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=fourth_token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=4,
            pid=os.getpid(),
            process_birth_identity="provider-worker-4",
            lease_token_sha256=fourth_token_sha256,
        )
        harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_TRANSIENT_AFTER_IDENTITY)}  # type: ignore[assignment]
        harness._resumed_children = {DISPATCH}
        assert harness._reap_children() is True
        exhausted_policy = ledger.retry_policy(DISPATCH)
        assert exhausted_policy["provider_transient_used"] == 3
        assert exhausted_policy["last_failure"] == "provider_transient"
        assert exhausted_policy["prior_thread_id"] == "provider-thread"
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.recovery_state(DISPATCH)["inspected_kind"] is None

        ledger.apply_recovery_action(
            DISPATCH,
            action_id="provider-fourth-recovery-grant",
            expected_revision=int(exhausted_policy["revision"]),
            action_kind=RecoveryActionKind.BUDGET_CHANGE,
            reason="authorize one final provider continuation",
            requested_budget=RetryBudgetChange(5, 1, 2, 1, 4),
        )
        granted_policy = ledger.retry_policy(DISPATCH)
        assert granted_policy["provider_transient_budget"] == 4
        assert granted_policy["provider_transient_used"] == 4
        assert harness._recover_exited_dispatch(ledger.queue_dispatch(DISPATCH)) is True
        granted_claim = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="e" * 64,
            now="9999-01-01T00:00:00Z",
        )
        assert granted_claim["attempt"] == 5
        assert granted_claim["thread_id"] == "provider-thread"
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 4

        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        fifth_token_sha256 = hashlib.sha256(b"provider-worker-5").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=5,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=fifth_token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=5,
            pid=os.getpid(),
            process_birth_identity="provider-worker-5",
            lease_token_sha256=fifth_token_sha256,
        )
        harness._children = {DISPATCH: _ExitedChild(WORKER_EXIT_TRANSIENT_AFTER_IDENTITY)}  # type: ignore[assignment]
        harness._resumed_children = {DISPATCH}
        assert harness._reap_children() is True
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.retry_policy(DISPATCH)["provider_transient_used"] == 4
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 4
        ledger.close()


def test_provider_transient_grant_is_exact_idempotent_and_preserves_usage() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        ledger._db().execute(
            "UPDATE dispatch_queue SET state = 'claimed', thread_id = 'provider-thread' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().execute(
            "UPDATE recovery_state SET recovery_state = 'none', inspection_used = 3, "
            "continuation_budget = 3, continuation_used = 3, inspected_thread_id = NULL, "
            "inspected_turn_id = NULL, inspected_kind = NULL, human_attention_reason = NULL "
            "WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().execute(
            "UPDATE retry_policies SET provider_transient_used = 3, last_failure = NULL, "
            "strategy = 'none', prior_thread_id = NULL, human_attention_reason = NULL "
            "WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        ledger.record_retry_failure(
            DISPATCH,
            failure=RetryFailureClass.PROVIDER_TRANSIENT,
            prior_thread_id="provider-thread",
        )
        revision = int(ledger.retry_policy(DISPATCH)["revision"])
        requested = RetryBudgetChange(5, 1, 2, 1, 4)

        action = ledger.apply_recovery_action(
            DISPATCH,
            action_id="provider-one-step-grant",
            expected_revision=revision,
            action_kind=RecoveryActionKind.BUDGET_CHANGE,
            reason="authorize one provider continuation",
            requested_budget=requested,
        )
        receipt = json.loads(str(action["requested_budget_json"]))
        assert receipt["provider_transient_budget"] == 4
        policy = ledger.retry_policy(DISPATCH)
        assert policy["provider_transient_used"] == 4
        assert policy["provider_transient_budget"] == 4
        assert policy["provider_transient_grant_used"] == 1
        recovery = ledger.recovery_state(DISPATCH)
        assert recovery["recovery_state"] == "recovery_inspection_pending"
        assert recovery["continuation_budget"] == 4
        assert recovery["continuation_used"] == 3
        assert ledger.queue_dispatch(DISPATCH)["state"] == "recovery_inspection_pending"

        repeated = ledger.apply_recovery_action(
            DISPATCH,
            action_id="provider-one-step-grant",
            expected_revision=revision,
            action_kind=RecoveryActionKind.BUDGET_CHANGE,
            reason="authorize one provider continuation",
            requested_budget=requested,
        )
        assert repeated["action_id"] == "provider-one-step-grant"
        assert ledger.retry_policy(DISPATCH)["provider_transient_grant_used"] == 1
        with pytest.raises(StaleWriter, match="revision is stale"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="provider-second-grant",
                expected_revision=revision,
                action_kind=RecoveryActionKind.BUDGET_CHANGE,
                reason="authorize a second provider continuation",
                requested_budget=requested,
            )

        ledger.record_recovery_inspection(
            DISPATCH,
            kind="transient_failed_turn",
            thread_id="provider-thread",
            turn_id="granted-failed-turn",
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        fourth = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=int(authority["epoch"]),
            claim_nonce_sha256="d" * 64,
            now="2000-01-01T00:00:01Z",
        )
        assert fourth["state"] == "claimed"
        assert fourth["thread_id"] == "provider-thread"
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 4

        ledger._db().execute(
            "UPDATE recovery_state SET recovery_state = 'recovery_continuation_pending', "
            "next_eligible_at = '2000-01-01T00:00:00Z' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().execute(
            "UPDATE dispatch_queue SET state = 'recovery_continuation_pending' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        exhausted = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=int(authority["epoch"]),
            claim_nonce_sha256="e" * 64,
            now="2000-01-01T00:00:02Z",
        )
        assert exhausted["state"] == "human_attention_required"
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 4
        ledger.close()


def test_provider_transient_grant_rejects_stale_facts_after_non_provider_attention() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute(
            "UPDATE dispatch_queue SET state = 'claimed', thread_id = 'provider-thread' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().execute(
            "UPDATE recovery_state SET recovery_state = 'none', inspection_used = 3, "
            "continuation_budget = 3, continuation_used = 3, inspected_thread_id = 'provider-thread', "
            "inspected_turn_id = 'stale-failed-turn', inspected_kind = 'transient_failed_turn' "
            "WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().execute(
            "UPDATE retry_policies SET provider_transient_used = 3, last_failure = 'provider_transient', "
            "strategy = 'none' WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()

        ledger.mark_human_attention_required(
            DISPATCH,
            reason="worker spawn failed before a durable live-worker binding",
        )

        policy = ledger.retry_policy(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        assert policy["last_failure"] is None
        assert recovery["inspected_kind"] is None
        revision = int(policy["revision"])
        with pytest.raises(StaleWriter, match="exact one-step authorization"):
            ledger.apply_recovery_action(
                DISPATCH,
                action_id="stale-provider-grant",
                expected_revision=revision,
                action_kind=RecoveryActionKind.BUDGET_CHANGE,
                reason="must not reuse stale provider facts",
                requested_budget=RetryBudgetChange(5, 1, 2, 1, 4),
            )
        assert ledger.retry_policy(DISPATCH)["provider_transient_budget"] == 3
        assert ledger.retry_policy(DISPATCH)["provider_transient_grant_used"] == 0
        assert ledger.recovery_state(DISPATCH)["continuation_budget"] == 3
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        ledger.close()


def test_human_attention_exit_arms_exactly_one_controller_checkpoint() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        ledger._db().execute(
            "UPDATE queue_bindings SET checkpoint_deadline = NULL, checkpoint_armed = 0 WHERE dispatch_id = ?",
            (DISPATCH,),
        )
        ledger._db().commit()
        harness = _configured_harness(ledger, root, epoch)
        harness._children = {DISPATCH: _ExitedChild(2)}  # type: ignore[assignment]

        assert harness._reap_children() is True
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_binding(DISPATCH)["checkpoint_armed"] == 1
        first = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:00Z")
        second = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:00Z")
        assert first is not None
        assert second is None
        ledger.close()


def test_explicit_retry_uses_fresh_thread_recovery_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.mark_human_attention_required(DISPATCH, reason="same-thread recovery exhausted")
        ledger.apply_recovery_action(
            DISPATCH,
            action_id="controller-authorized-fresh-retry",
            expected_revision=int(ledger.retry_policy(DISPATCH)["revision"]),
            action_kind=RecoveryActionKind.RETRY,
            reason="continue from retained worktree changes in a fresh thread",
        )
        harness = _configured_harness(ledger, root, epoch)
        captured: list[tuple[int, bool]] = []

        def capture(row: dict[str, object], *, recovery_continuation: bool = False) -> None:
            captured.append((int(row["attempt"]), recovery_continuation))

        monkeypatch.setattr(harness, "_spawn_one", capture)
        assert harness.process_once() is True
        assert captured == [(2, True)]

        original_prompt = "Implement the original durable milestone objective."
        same_thread = recovery_continuation_prompt(
            workspace=root,
            resume_same_thread=True,
            original_prompt=original_prompt,
        )
        fresh_thread = recovery_continuation_prompt(
            workspace=root,
            resume_same_thread=False,
            original_prompt=original_prompt,
        )
        assert "retains changes made by the prior worker" in same_thread
        assert "Resume the existing SDK thread" in same_thread
        assert "retains changes made by the prior worker" in fresh_thread
        assert "fresh SDK thread replacing a failed execution" in fresh_thread
        assert "recover context from the worktree" in fresh_thread
        assert "this dispatch's active row" in fresh_thread
        assert "not conflicting owners" in fresh_thread
        assert same_thread.endswith(original_prompt)
        assert fresh_thread.endswith(original_prompt)
        ledger.close()


def test_controller_action_submission_immediately_triggers_queue_processing() -> None:
    assert "controller_submit_actions" in QUEUE_TRIGGERING_OPERATIONS
    assert "controller_submit_recovered_actions" in QUEUE_TRIGGERING_OPERATIONS
    assert "controller_acknowledge" not in QUEUE_TRIGGERING_OPERATIONS


def test_worker_exit_recovery_inspects_once_after_event_reap() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="event-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute(
            "UPDATE dispatch_queue SET thread_id = 'persisted-thread' WHERE dispatch_id = ?", (DISPATCH,)
        )
        ledger._db().commit()
        calls: list[str] = []
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = epoch
        harness._children = {DISPATCH: _ExitedChild(2)}
        harness._resumed_children = set()
        harness._thread_inspector = lambda thread_id: (
            calls.append(thread_id) or ThreadInspection(thread_id, ThreadInspectionKind.IDLE_NO_RESULT)
        )

        assert harness._reap_children() is True
        harness.recover_once()

        assert calls == ["persisted-thread"]
        # The low-level fixture intentionally omits production permission
        # facts, so continuation preparation fails closed.  The harness
        # must not leave a claimed/none row behind after that failure.
        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.queue_dispatch(DISPATCH)["attempt"] == 2
        assert ledger.recovery_state(DISPATCH)["continuation_used"] == 1
        ledger.close()


def test_worker_exit_decision_is_crash_atomic_and_terminal_inspection_requires_payload() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="exit-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._fault_injector = lambda stage: (
            (_ for _ in ()).throw(RuntimeError("injected worker-exit crash")) if stage == "after_worker_exit" else None
        )
        with pytest.raises(RuntimeError, match="worker-exit crash"):
            ledger.mark_worker_exit(
                DISPATCH,
                pid=os.getpid(),
                process_birth_identity="exit-worker",
                exit_code=2,
                classification="worker-exit",
            )
        ledger._fault_injector = None
        assert ledger.queue_dispatch(DISPATCH)["state"] == "starting"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is None  # type: ignore[index]
        assert ledger.recovery_state(DISPATCH)["recovery_state"] == "none"
        with pytest.raises(ValueError, match="terminal result inspection"):
            ledger.record_recovery_inspection(DISPATCH, kind="terminal_result")
        with pytest.raises(ValueError, match="recovery decision state is unsupported"):
            ledger.record_recovery_inspection(DISPATCH, kind="idle_no_result", next_state="completed")
        with pytest.raises(ValueError, match="recovery decision state is unsupported"):
            ledger.record_recovery_inspection(DISPATCH, kind="idle_no_result", next_state="failed")
        ledger.close()


def test_transport_before_identity_exit_returns_the_atomic_requeued_row() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="pre-identity-worker",
            lease_token_sha256=token_sha256,
        )

        returned = ledger.mark_worker_exit(
            DISPATCH,
            pid=os.getpid(),
            process_birth_identity="pre-identity-worker",
            classification="transport-before-identity",
        )
        assert returned["state"] == "queued"
        assert returned["attempt"] == 2
        assert ledger.worker_liveness(DISPATCH) is None
        assert ledger.retry_policy(DISPATCH)["pre_identity_used"] == 1
        ledger.close()


def test_reap_retries_failed_exit_transition_without_stranding_same_epoch_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="retry-exit-worker",
            lease_token_sha256=token_sha256,
        )
        child = _ExitedChild(2)
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = epoch
        harness._children = {DISPATCH: child}
        harness._resumed_children = set()
        original = ledger.mark_worker_exit
        calls = 0

        def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LedgerError("transient exit ledger failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(ledger, "mark_worker_exit", fail_once)

        assert harness._reap_children() is False
        assert harness._children == {DISPATCH: child}
        assert ledger.queue_dispatch(DISPATCH)["state"] == "starting"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is None  # type: ignore[index]

        assert harness._reap_children() is True
        assert harness._children == {}
        assert calls == 2
        assert ledger.queue_dispatch(DISPATCH)["state"] == "recovery_inspection_pending"
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is not None  # type: ignore[index]
        ledger.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("generation", True), ("attempt", True), ("turn_id", "bad\nturn"), ("turn_id", " " * 2)],
)
def test_worker_turn_identity_is_rejected_before_active_turn_mutation(field: str, value: object) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'thread-1' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        harness = _configured_harness(ledger, root, epoch)
        harness._active_turns = {}
        payload: dict[str, object] = {
            "version": 1,
            "operation": "bind_turn",
            "dispatch_id": DISPATCH,
            "generation": 1,
            "attempt": 1,
            "token": token,
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }
        payload[field] = value

        with pytest.raises(IpcError, match="worker"):
            harness._bind_turn(payload)
        assert harness._active_turns == {}
        ledger.close()


@pytest.mark.parametrize("operation", ["bind_turn", "worker_event", "poll_commands", "ack_control"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", True),
        ("generation", 1.0),
        ("attempt", True),
        ("attempt", 1.0),
        ("thread_id", True),
        ("turn_id", True),
        ("turn_id", "bad\nturn"),
        ("turn_id", " " * 2),
        ("turn_id", "t" * 513),
    ],
)
def test_malformed_worker_operation_identity_leaves_liveness_activity_and_active_turn_unchanged(
    operation: str,
    field: str,
    value: object,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="strict-identity-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'thread-1' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        harness = _configured_harness(ledger, root, epoch)
        harness._active_turns = {DISPATCH: (1, 1, "thread-1", "turn-1")}
        payload: dict[str, object] = {
            "version": 1,
            "operation": operation,
            "dispatch_id": DISPATCH,
            "generation": 1,
            "attempt": 1,
            "token": token,
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }
        if operation == "worker_event":
            payload.update({"sequence": 1, "kind": "agent_message", "text": "bounded activity"})
        elif operation == "ack_control":
            payload.update(
                {
                    "command_id": "strict-identity-command",
                    "kind": "steer",
                    "state": "acknowledged",
                    "detail": None,
                }
            )
        payload[field] = value

        def snapshot() -> tuple[bytes, bytes, bytes]:
            return (
                json.dumps(ledger.worker_liveness(DISPATCH), sort_keys=True).encode("utf-8"),
                json.dumps(ledger.recent_activity(DISPATCH), sort_keys=True).encode("utf-8"),
                json.dumps(harness._active_turns, sort_keys=True).encode("utf-8"),
            )

        before = snapshot()
        handler = {
            "bind_turn": harness._bind_turn,
            "worker_event": harness._worker_event,
            "poll_commands": harness._poll_commands,
            "ack_control": harness._ack_control,
        }[operation]
        with pytest.raises(IpcError):
            handler(payload)
        assert snapshot() == before
        ledger.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", True),
        ("generation", 1.0),
        ("attempt", True),
        ("attempt", 1.0),
        ("thread_id", True),
        ("thread_id", "bad\nthread"),
    ],
)
def test_malformed_worker_binding_leaves_queue_and_worker_ownership_unchanged(field: str, value: object) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="strict-binding-worker",
            lease_token_sha256=token_sha256,
        )
        harness = _configured_harness(ledger, root, epoch)
        harness._active_turns = {}
        payload: dict[str, object] = {
            "version": 1,
            "operation": "bind_worker",
            "dispatch_id": DISPATCH,
            "generation": 1,
            "attempt": 1,
            "token": token,
            "thread_id": "thread-1",
        }
        payload[field] = value

        def snapshot() -> bytes:
            return json.dumps(
                {
                    "queue": ledger.queue_dispatch(DISPATCH),
                    "liveness": ledger.worker_liveness(DISPATCH),
                    "activity": ledger.recent_activity(DISPATCH),
                    "active_turns": harness._active_turns,
                },
                sort_keys=True,
            ).encode("utf-8")

        before = snapshot()
        with pytest.raises(IpcError):
            harness._bind_worker(payload)
        assert snapshot() == before
        ledger.close()


def test_broken_rejection_peer_cannot_escape_the_harness_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenPeer:
        closed = False

        def settimeout(self, _timeout: float) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            raise BrokenPipeError("peer closed")

        def close(self) -> None:
            self.closed = True

    harness = object.__new__(WorkflowHarness)
    harness.lease_seconds = 30.0
    peer = _BrokenPeer()
    monkeypatch.setattr(harness_module, "peer_uid", lambda _connection: os.getuid())
    monkeypatch.setattr(
        harness_module,
        "decode_frame",
        lambda _connection: {"version": 1, "operation": "unsupported"},
    )

    harness._accept_connection(peer)  # type: ignore[arg-type]
    assert peer.closed is True


def test_oversized_response_is_bounded_and_next_request_remains_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(WorkflowHarness)
    harness.lease_seconds = 30.0
    harness._next_renewal_monotonic = None

    def response(request: dict[str, object]) -> dict[str, object]:
        if request["operation"] == "status":
            return {"version": 1, "ok": True, "payload": "x" * 70_000}
        return {"version": 1, "ok": True, "operation": request["operation"]}

    monkeypatch.setattr(harness, "_request_ack", response)
    monkeypatch.setattr(harness, "_renew_harness_lease_if_due", lambda: None)

    first_client, first_server = socket.socketpair()
    try:
        first_client.sendall(encode_frame({"version": 1, "operation": "status"}))
        assert harness._accept_connection(first_server) == "status"
        assert decode_frame(first_client) == {"version": 1, "ok": False, "error": "response_too_large"}
    finally:
        first_client.close()

    second_client, second_server = socket.socketpair()
    try:
        second_client.sendall(encode_frame({"version": 1, "operation": "wake"}))
        assert harness._accept_connection(second_server) == "wake"
        assert decode_frame(second_client) == {"version": 1, "ok": True, "operation": "wake"}
    finally:
        second_client.close()


def test_status_compacts_large_diagnostic_history_before_ipc() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        harness = _configured_harness(ledger, root, int(authority["epoch"]))
        for sequence in range(1, 17):
            ledger.append_diagnostic(
                DISPATCH,
                kind="large_diagnostic",
                text=f"event-{sequence}:" + ("x" * 8_000),
                sequence=sequence,
            )

        client, server = socket.socketpair()
        try:
            client.sendall(encode_frame({"version": 1, "operation": "status"}))
            assert harness._accept_connection(server) == "status"
            response = decode_frame(client)
        finally:
            client.close()

        assert response["ok"] is True
        queue = response["queue"]
        assert isinstance(queue, list) and len(queue) == 1
        activity = queue[0]["recent_activity"]
        assert [item["sequence"] for item in activity] == [13, 14, 15, 16]
        assert all(len(item["text"].encode("utf-8")) <= 512 for item in activity)
        harness.close()


def test_conversation_response_overflow_returns_bounded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = object.__new__(WorkflowHarness)
    monkeypatch.setattr(
        harness,
        "_request_ack",
        lambda _request: {"version": 1, "ok": True, "page": {"fragments": ["x" * 70_000]}},
    )
    client, server = socket.socketpair()
    try:
        harness._serve_conversation_connection(
            server,
            {"version": 1, "operation": "conversation_history"},
        )
        assert decode_frame(client) == {"version": 1, "ok": False, "error": "response_too_large"}
    finally:
        client.close()


def test_worker_event_storm_keeps_short_harness_lease_and_terminal_result_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root, lease_seconds=1.0)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "event-storm-worker"
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
            lease_seconds=1,
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="event-storm-worker",
            lease_token_sha256=token_hash,
            lease_seconds=1,
        )
        ledger.bind_worker_thread(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            token=token,
            thread_id="thread-storm",
        )
        harness = _configured_harness(ledger, root, epoch)
        harness.lease_seconds = 1.0
        harness._active_turns = {DISPATCH: (1, 1, "thread-storm", "turn-storm")}
        whole_ledger_validations = 0
        original = ledger._validate_rows

        def counted_validation() -> None:
            nonlocal whole_ledger_validations
            whole_ledger_validations += 1
            original()

        monkeypatch.setattr(ledger, "_validate_rows", counted_validation)
        for sequence in range(1, 2_001):
            harness._renew_harness_lease_if_due()
            text = "x" * 64
            response = harness._worker_event(
                {
                    "version": 1,
                    "operation": "worker_event",
                    "dispatch_id": DISPATCH,
                    "generation": 1,
                    "attempt": 1,
                    "token": token,
                    "thread_id": "thread-storm",
                    "turn_id": "turn-storm",
                    "sequence": sequence,
                    "kind": "item/commandExecution/outputDelta",
                    "text": text,
                    "payload_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
            assert response["ok"] is True
        assert response["event"] is None
        assert response["evicted"] is True
        assert whole_ledger_validations < 32
        live_authority = ledger.harness_authority()
        assert live_authority is not None
        assert int(live_authority["epoch"]) == epoch
        assert int(live_authority["pid"]) == os.getpid()
        assert str(live_authority["expires_at"]) > utc_now()

        terminal = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )
        replay = ledger.commit_queue_result(
            DISPATCH,
            generation=1,
            attempt=1,
            token=token,
            raw_result=_completed_worker_result(),
        )
        assert terminal == replay
        assert terminal["state"] == "completed"
        assert (
            ledger._db()
            .execute("SELECT COUNT(*) FROM successor_outbox WHERE source_dispatch_id = ?", (DISPATCH,))
            .fetchone()[0]
            == 1
        )
        assert (
            ledger._db()
            .execute("SELECT COUNT(*) FROM notification_outbox WHERE dispatch_id = ?", (DISPATCH,))
            .fetchone()[0]
            == 1
        )
        assert (
            ledger._db().execute("SELECT COUNT(*) FROM wake_outbox WHERE dispatch_id = ?", (DISPATCH,)).fetchone()[0]
            == 1
        )
        ledger.close()


def test_high_volume_worker_events_are_lossy_but_terminal_boundaries_are_not() -> None:
    methods = [
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "thread/tokenUsage/updated",
        "turn/diff/updated",
    ]
    assert all(
        worker_module._is_lossy_worker_diagnostic(worker_module.LifecycleEvent(index, method, "turn-1", "noisy"))
        for index, method in enumerate(methods * 1_000, start=1)
    )
    assert not worker_module._is_lossy_worker_diagnostic(
        worker_module.LifecycleEvent(4_001, "item/completed", "turn-1", "complete")
    )
    assert not worker_module._is_lossy_worker_diagnostic(
        worker_module.LifecycleEvent(4_002, "turn/completed", "turn-1")
    )


def test_live_keyframe_broker_is_ephemeral_bounded_identity_bound_and_terminal_prioritized(
    tmp_path: Path,
) -> None:
    ledger = _queued_ledger(tmp_path)
    authority = _harness(ledger, tmp_path)
    epoch = int(authority["epoch"])
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    token = "live-keyframe-worker"
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    ledger.issue_attempt_capability(
        DISPATCH,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=token_hash,
        expires_at="9999-12-31T23:59:59Z",
    )
    ledger.bind_worker_liveness(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        pid=os.getpid(),
        process_birth_identity="live-keyframe-worker",
        lease_token_sha256=token_hash,
    )
    ledger.bind_worker_thread(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        token=token,
        thread_id="live-thread",
    )
    harness = _configured_harness(ledger, tmp_path, epoch)
    harness._active_turns = {DISPATCH: (1, 1, "live-thread", "live-turn")}
    request = {
        "version": 1,
        "operation": "live_subscribe",
        "dispatch_id": DISPATCH,
        "generation": 1,
        "attempt": 1,
        "thread_id": "live-thread",
        "turn_id": "live-turn",
    }
    subscriber = harness._live_subscription(request)
    before = "\n".join(ledger._db().iterdump())

    for revision in range(1, 65):
        keyframe = LiveTurnKeyframe(
            DISPATCH,
            1,
            1,
            ThreadIdentity("live-thread"),
            "live-turn",
            revision,
            f"assistant revision {revision} token=[REDACTED]",
        )
        response = harness._publish_live_keyframe(
            {
                "version": 1,
                "operation": "live_keyframe",
                "dispatch_id": DISPATCH,
                "generation": 1,
                "attempt": 1,
                "token": token,
                "thread_id": "live-thread",
                "turn_id": "live-turn",
                "keyframe": keyframe.to_json(),
            }
        )
        assert response["ok"] is True

    assert subscriber.frames.qsize() == 8
    newest = subscriber.frames.queue[-1]
    assert newest.live_revision == 64
    assert "token=[REDACTED]" in newest.assistant_text
    replay = harness._publish_live_keyframe(
        {
            "version": 1,
            "operation": "live_keyframe",
            "dispatch_id": DISPATCH,
            "generation": 1,
            "attempt": 1,
            "token": token,
            "thread_id": "live-thread",
            "turn_id": "live-turn",
            "keyframe": LiveTurnKeyframe(
                DISPATCH,
                1,
                1,
                ThreadIdentity("live-thread"),
                "live-turn",
                1,
                "replayed token=[REDACTED]",
            ).to_json(),
        }
    )
    assert replay == {"version": 1, "ok": True, "operation": "live_keyframe", "delivered": 0}
    assert subscriber.frames.queue[-1].live_revision == 64
    assert "private" not in json.dumps(ledger.recent_activity(DISPATCH))
    assert "\n".join(ledger._db().iterdump()) == before

    client, server = socket.socketpair()
    stream = threading.Thread(target=harness._serve_live_connection, args=(server, subscriber), daemon=True)
    stream.start()
    assert decode_frame(client) == {"version": 1, "ok": True, "operation": "live_subscribe"}
    harness._close_live_subject(DISPATCH, (1, 1, "live-thread", "live-turn"))
    terminal = decode_frame(client)
    assert terminal["event"] == "terminal"
    stream.join(timeout=2)
    assert not stream.is_alive()
    client.close()
    ledger.close()


def test_foreground_worker_event_volume_does_not_rescan_and_result_ingress_stays_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = _queued_ledger(tmp_path)
    authority = _harness(ledger, tmp_path)
    epoch = int(authority["epoch"])
    harness = _configured_harness(ledger, tmp_path, epoch)
    harness.lease_seconds = 30.0
    token = "foreground-event-token"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="b" * 64)
    ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
    ledger.issue_attempt_capability(
        DISPATCH,
        generation=1,
        attempt=1,
        operation="submit_result",
        schema_sha256=model_facing_result_schema_sha256(),
        workspace_path=tmp_path,
        backend="sdk_headless",
        token_sha256=token_hash,
        expires_at="9999-12-31T23:59:59Z",
    )
    ledger.bind_worker_liveness(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        pid=os.getpid(),
        process_birth_identity="foreground-event-worker",
        lease_token_sha256=token_hash,
    )
    ledger.bind_worker_thread(
        DISPATCH,
        epoch=epoch,
        generation=1,
        attempt=1,
        token=token,
        thread_id="foreground-event-thread",
    )
    harness._active_turns = {DISPATCH: (1, 1, "foreground-event-thread", "foreground-event-turn")}

    class ReadyEndpoint:
        def accept(self) -> tuple[object, None]:
            return object(), None

    endpoint = ReadyEndpoint()
    accepted = 0
    scheduling_calls = 0
    queue_triggers = 0
    terminal_ingress = 0

    def fake_acquire() -> None:
        harness.epoch = epoch
        harness._socket = endpoint  # type: ignore[assignment]

    def fake_accept(_connection: object) -> str:
        nonlocal accepted, terminal_ingress
        accepted += 1
        if accepted <= 200:
            text = "progress"
            response = harness._worker_event(
                {
                    "version": 1,
                    "operation": "worker_event",
                    "dispatch_id": DISPATCH,
                    "generation": 1,
                    "attempt": 1,
                    "token": token,
                    "thread_id": "foreground-event-thread",
                    "turn_id": "foreground-event-turn",
                    "sequence": accepted,
                    "kind": "item/commandExecution/outputDelta",
                    "text": text,
                    "payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            assert response == {"version": 1, "ok": True, "event": None, "evicted": True}
            return "worker_event"
        terminal_ingress += 1
        response = harness._submit_result(
            {
                "version": 1,
                "operation": "submit_result",
                "dispatch_id": DISPATCH,
                "generation": 1,
                "attempt": 1,
                "backend": "sdk_headless",
                "workspace_path": str(tmp_path),
                "schema_sha256": model_facing_result_schema_sha256(),
                "token": token,
                "raw_result": _completed_worker_result(),
            }
        )
        assert response["ok"] is True
        return "submit_result"

    def fake_process_queue_event() -> bool:
        nonlocal queue_triggers
        queue_triggers += 1
        return False

    def count_schedule() -> bool:
        nonlocal scheduling_calls
        scheduling_calls += 1
        return False

    monkeypatch.setattr(harness, "acquire", fake_acquire)
    monkeypatch.setattr(harness, "close", lambda: None)
    monkeypatch.setattr(harness, "recover_once", lambda: None)
    monkeypatch.setattr(harness, "_accept_connection", fake_accept)
    monkeypatch.setattr(harness, "_process_queue_event", fake_process_queue_event)
    monkeypatch.setattr(harness, "_schedule_controller_generations", count_schedule)
    monkeypatch.setattr(harness, "_deliver_wakes", lambda: False)
    monkeypatch.setattr(harness, "_refresh_checkpoint_deadline", lambda: None)
    monkeypatch.setattr(harness, "_renew_harness_lease_if_due", lambda: None)
    monkeypatch.setattr(harness, "_reap_children", lambda: False)
    monkeypatch.setattr(harness, "_reap_controller_generations", lambda: False)
    monkeypatch.setattr(harness, "_select_timeout", lambda _timeout: 0.0)
    monkeypatch.setattr(harness_module.select, "select", lambda readable, _write, _error, _timeout: (readable, [], []))

    harness.run_foreground(timeout=0.0, max_cycles=201)

    assert accepted == 201
    assert terminal_ingress == 1
    assert queue_triggers == 2
    # Startup and the one terminal queue event are lifecycle triggers; the
    # 200 noisy worker events do not perform another program/controller scan.
    assert scheduling_calls == 2
    assert ledger.queue_dispatch(DISPATCH)["state"] == "completed"
    ledger.close()


def test_worker_diagnostic_fails_closed_after_explicit_harness_authority_loss() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "lost-authority-worker"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="lost-authority-worker",
            lease_token_sha256=token_hash,
        )
        ledger._db().execute("UPDATE harness_authority SET expires_at = '2000-01-01T00:00:00Z' WHERE singleton = 1")
        ledger._db().commit()
        with pytest.raises(StaleWriter, match="live harness lease"):
            ledger.append_worker_diagnostic(
                DISPATCH,
                generation=1,
                attempt=1,
                token=token,
                epoch=epoch,
                lease_seconds=1,
                kind="turn/started",
                text=None,
                payload_sha256=None,
                sequence=1,
            )
        assert ledger.recent_activity(DISPATCH) == ()
        ledger.close()


def test_dead_idle_recovery_consumes_one_bounded_continuation_without_resetting_attempt() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="idle-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        ledger.mark_worker_exit(DISPATCH, pid=os.getpid(), process_birth_identity="idle-worker")
        ledger.record_recovery_inspection(
            DISPATCH,
            kind="idle_no_result",
            thread_id="old-thread",
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="2000-01-02T00:00:00Z",
        )
        assert claimed["state"] == "claimed"
        assert claimed["attempt"] == 2
        assert claimed["thread_id"] == "old-thread"
        recovery = ledger.recovery_state(DISPATCH)
        assert recovery["continuation_used"] == 1
        assert recovery["fresh_thread_used"] == 0
        ledger.close()


def test_restart_observed_dead_worker_inspects_and_resumes_the_same_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        first = _harness(ledger, root)
        first_epoch = int(first["epoch"])
        ledger.claim_queue_dispatch(epoch=first_epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=first_epoch)
        token_hash = hashlib.sha256(b"dead-worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_hash,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=first_epoch,
            generation=1,
            attempt=1,
            pid=2_147_483_647,
            process_birth_identity="dead-worker",
            lease_token_sha256=token_hash,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().execute("UPDATE harness_authority SET expires_at = '2000-01-01T00:00:00Z'")
        ledger._db().commit()
        replacement = ledger.acquire_harness(
            repository_root=root,
            state_root=root,
            pid=os.getpid(),
            process_birth_identity="replacement-harness",
            executable_digest="c" * 64,
            version="test",
            owner_nonce_sha256="d" * 64,
        )
        harness = _configured_harness(ledger, root, int(replacement["epoch"]))
        inspected: list[str] = []

        def inspect(thread_id: str) -> ThreadInspection:
            inspected.append(thread_id)
            return ThreadInspection(thread_id, ThreadInspectionKind.IDLE_NO_RESULT)

        harness._thread_inspector = inspect
        child = _LiveChild()
        monkeypatch.setattr(harness_module.subprocess, "Popen", lambda *args, **kwargs: child)

        harness.recover_once()

        queue = ledger.queue_dispatch(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        retry = ledger.retry_policy(DISPATCH)
        assert queue["state"] == "starting", (queue, recovery, retry)
        assert queue["attempt"] == 2
        assert queue["thread_id"] == "old-thread"
        assert inspected == ["old-thread"]
        assert recovery["worker_exit_classification"] == "restart-observed-worker-dead"
        assert recovery["recovery_state"] == "none"
        assert recovery["continuation_used"] == 1
        assert harness._children[DISPATCH] is child
        ledger.close()


def test_real_spawn_continuation_uses_new_monotonic_attempt_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation resumes the same thread without reusing g1-a1 files."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        _prepare_idle_continuation(ledger, root, epoch)
        harness = _configured_harness(ledger, root, epoch)
        harness._thread_inspector = lambda thread_id: ThreadInspection(thread_id, ThreadInspectionKind.IDLE_NO_RESULT)
        old_digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        for prefix in ("capability", "capsule", "result"):
            (harness.runtime_root / f"{prefix}-{old_digest}-g1-a1.json").write_text("retained", encoding="utf-8")
        child = _LiveChild()
        monkeypatch.setattr(harness_module.subprocess, "Popen", lambda *args, **kwargs: child)

        harness.recover_once()

        queue = ledger.queue_dispatch(DISPATCH)
        assert queue["state"] == "starting"
        assert queue["attempt"] == 2
        assert queue["thread_id"] == "old-thread"
        assert ledger.recovery_state(DISPATCH)["recovery_state"] == "none"
        liveness = ledger.worker_liveness(DISPATCH)
        assert liveness is not None
        assert liveness["attempt"] == 2
        assert harness.runtime_root.joinpath(f"capability-{old_digest}-g1-a2.json").exists()
        assert harness.runtime_root.joinpath(f"capsule-{old_digest}-g1-a2.json").exists()
        assert not harness.runtime_root.joinpath(f"result-{old_digest}-g1-a2.json").exists()
        capability_attempts = (
            ledger._db()
            .execute("SELECT generation, attempt FROM attempt_capabilities WHERE dispatch_id = ?", (DISPATCH,))
            .fetchall()
        )
        assert [(int(item[0]), int(item[1])) for item in capability_attempts] == [(1, 2)]
        assert harness._children[DISPATCH] is child
        ledger.close()


def test_spawn_failure_closes_continuation_without_claimed_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Popen leaves no active claim that restart could duplicate."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        _prepare_idle_continuation(ledger, root, epoch)
        harness = _configured_harness(ledger, root, epoch)
        monkeypatch.setattr(
            harness_module.subprocess,
            "Popen",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected launch failure")),
        )

        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="2000-01-02T00:00:00Z",
        )
        with pytest.raises(HarnessError, match="worker launch failed"):
            harness._spawn_one(claimed, recovery_continuation=True)

        queue = ledger.queue_dispatch(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        assert queue["state"] == "human_attention_required"
        assert recovery["recovery_state"] == "human_attention_required"
        assert queue["attempt"] == 2
        assert ledger.worker_liveness(DISPATCH) is None
        digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        assert not harness.runtime_root.joinpath(f"capability-{digest}-g1-a2.json").exists()
        assert not harness.runtime_root.joinpath(f"capsule-{digest}-g1-a2.json").exists()
        ledger.close()


@pytest.mark.parametrize("generation_state", ["delivery_starting", "active"])
def test_controller_recovery_launch_failure_closes_unclaimed_orphan(
    monkeypatch: pytest.MonkeyPatch, generation_state: str
) -> None:
    """A restart orphan is durably ambiguous instead of silently abandoned."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        ledger.prepare_controller_generation(decision.decision_id, generation=1)
        if generation_state == "active":
            ledger.bind_controller_generation_thread(
                decision.decision_id, generation=1, controller_thread_id="orphan-controller-thread"
            )
        status = ledger.controller_decision(decision.decision_id)
        generation = ledger.controller_generation(decision.decision_id)
        assert status.state.value == "awaiting_claim"
        assert generation.state.value == generation_state
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        monkeypatch.setattr(
            harness_module.subprocess,
            "Popen",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected controller launch failure")),
        )

        assert harness._spawn_controller_generation(status, recovery=True) is False

        closed = ledger.controller_decision(decision.decision_id)
        orphan = ledger.controller_generation(decision.decision_id)
        assert closed.state is ControllerDecisionState.HUMAN_ATTENTION_REQUIRED
        assert closed.claimant_id is None
        assert orphan.state is ControllerGenerationState.AMBIGUOUS
        assert orphan.inspection_outcome == ControllerGenerationState.AMBIGUOUS.value
        ledger.close()


def test_controller_recovery_launch_failure_does_not_clear_newer_human_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orphan CAS leaves a human claim acquired during launch untouched."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        ledger.prepare_controller_generation(decision.decision_id, generation=1)
        status = ledger.controller_decision(decision.decision_id)
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))

        def claim_then_fail(*args: object, **kwargs: object) -> object:
            ledger.claim_controller_decision(
                decision.decision_id,
                claimant_kind="human",
                claimant_id="operator",
                expected_revision=status.revision,
                now="2025-01-01T00:00:01Z",
            )
            raise OSError("injected controller launch failure")

        monkeypatch.setattr(harness_module.subprocess, "Popen", claim_then_fail)
        assert harness._spawn_controller_generation(status, recovery=True) is False

        claimed = ledger.controller_decision(decision.decision_id)
        orphan = ledger.controller_generation(decision.decision_id)
        assert claimed.state is ControllerDecisionState.CLAIMED
        assert claimed.claimant_kind is ControllerClaimantKind.HUMAN
        assert claimed.claimant_id == "operator"
        assert claimed.revision == status.revision + 1
        assert orphan.state is ControllerGenerationState.DELIVERY_STARTING
        assert orphan.inspection_outcome is None
        ledger.close()


@pytest.mark.parametrize("recovery", [False, True], ids=["initial", "recovery"])
def test_controller_launch_reaps_child_when_human_claim_wins_during_popen(
    monkeypatch: pytest.MonkeyPatch,
    recovery: bool,
) -> None:
    """A successful Popen cannot outlive ownership lost inside that call."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        if recovery:
            ledger.prepare_controller_generation(decision.decision_id, generation=1)
        status = ledger.controller_decision(decision.decision_id)
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        child = _StoppingChild()

        def claim_then_start(*args: object, **kwargs: object) -> _StoppingChild:
            ledger.claim_controller_decision(
                decision.decision_id,
                claimant_kind=ControllerClaimantKind.HUMAN,
                claimant_id="operator",
                expected_revision=status.revision,
            )
            return child

        monkeypatch.setattr(harness_module.subprocess, "Popen", claim_then_start)

        assert harness._spawn_controller_generation(status, recovery=recovery) is False

        claimed = ledger.controller_decision(decision.decision_id)
        assert claimed.state is ControllerDecisionState.CLAIMED
        assert claimed.claimant_kind is ControllerClaimantKind.HUMAN
        assert claimed.claimant_id == "operator"
        assert child.terminated is True
        assert child.return_code == -15
        assert str(decision.decision_id) not in harness._controller_children
        assert ledger.controller_generation(decision.decision_id).state is ControllerGenerationState.DELIVERY_STARTING
        ledger.close()


@pytest.mark.parametrize("recovery", [False, True], ids=["initial", "recovery"])
def test_controller_launch_registers_child_only_after_unchanged_authority_bind(
    monkeypatch: pytest.MonkeyPatch,
    recovery: bool,
) -> None:
    """The positive launch path survives the same post-Popen ownership CAS."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        if recovery:
            ledger.prepare_controller_generation(decision.decision_id, generation=1)
        status = ledger.controller_decision(decision.decision_id)
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        child = _LiveChild()
        monkeypatch.setattr(harness_module.subprocess, "Popen", lambda *args, **kwargs: child)

        assert harness._spawn_controller_generation(status, recovery=recovery) is True

        assert harness._controller_children[str(decision.decision_id)] is child
        assert ledger.controller_decision(decision.decision_id) == status
        assert ledger.controller_generation(decision.decision_id).state is ControllerGenerationState.DELIVERY_STARTING
        ledger.close()


def test_controller_recovery_launch_reaps_child_when_model_claim_revision_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-Popen bind includes the exact model capability revision."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        ledger.prepare_controller_generation(decision.decision_id, generation=1)
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.MODEL,
            claimant_id="controller-generation/source",
            expected_revision=decision.revision,
        )
        status = ledger.controller_decision(decision.decision_id)
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        child = _StoppingChild()

        def renew_then_start(*args: object, **kwargs: object) -> _StoppingChild:
            ledger.renew_controller_decision_claim(
                decision.decision_id,
                claimant_id=claim.claimant_id,
                token=str(claim.token),
                expected_revision=claim.revision,
            )
            return child

        monkeypatch.setattr(harness_module.subprocess, "Popen", renew_then_start)

        assert harness._spawn_controller_generation(status, recovery=True) is False

        renewed = ledger.controller_decision(decision.decision_id)
        assert renewed.state is ControllerDecisionState.CLAIMED
        assert renewed.claimant_kind is ControllerClaimantKind.MODEL
        assert renewed.claimant_id == claim.claimant_id
        assert renewed.revision == claim.revision + 1
        assert child.terminated is True
        assert str(decision.decision_id) not in harness._controller_children
        ledger.close()


@pytest.mark.parametrize("recovery", [False, True], ids=["initial", "recovery"])
def test_controller_launch_reaps_child_when_generation_state_drifts(
    monkeypatch: pytest.MonkeyPatch,
    recovery: bool,
) -> None:
    """The post-Popen bind rejects a stale generation as well as claim drift."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        if recovery:
            ledger.prepare_controller_generation(decision.decision_id, generation=1)
        status = ledger.controller_decision(decision.decision_id)
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        child = _StoppingChild()

        def finish_then_start(*args: object, **kwargs: object) -> _StoppingChild:
            ledger.complete_controller_generation(
                decision.decision_id,
                generation=1,
                state=ControllerGenerationState.FAILED,
            )
            return child

        monkeypatch.setattr(harness_module.subprocess, "Popen", finish_then_start)

        assert harness._spawn_controller_generation(status, recovery=recovery) is False

        assert ledger.controller_decision(decision.decision_id).state is ControllerDecisionState.AWAITING_CLAIM
        assert ledger.controller_generation(decision.decision_id).state is ControllerGenerationState.FAILED
        assert child.terminated is True
        assert child.return_code == -15
        assert str(decision.decision_id) not in harness._controller_children
        ledger.close()


@pytest.mark.parametrize("generation_state", ["delivery_starting", "active"])
def test_human_claim_never_launches_or_recovers_a_model_generation(
    monkeypatch: pytest.MonkeyPatch, generation_state: str
) -> None:
    """Human decision ownership cannot be mistaken for model-writer ownership."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        ledger.prepare_controller_generation(decision.decision_id, generation=1)
        if generation_state == "active":
            ledger.bind_controller_generation_thread(
                decision.decision_id,
                generation=1,
                controller_thread_id="human-owned-controller-thread",
            )
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=decision.revision,
        )
        assert claim.claimant_kind is ControllerClaimantKind.HUMAN
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        launches: list[tuple[object, bool]] = []
        monkeypatch.setattr(
            harness,
            "_spawn_controller_generation",
            lambda status, *, recovery=False: launches.append((status, recovery)) or True,
        )

        harness._schedule_controller_generations()

        assert launches == []
        claimed = ledger.controller_decision(decision.decision_id)
        assert claimed.state is ControllerDecisionState.CLAIMED
        assert claimed.claimant_kind is ControllerClaimantKind.HUMAN
        assert ledger.controller_generation(decision.decision_id).state.value == generation_state
        ledger.close()


def test_committed_human_attention_is_scheduled_only_until_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart ack is durable and cannot become a repeated child launch."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        decision = ledger.create_controller_decision(DISPATCH, kind="checkpoint")
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.HUMAN,
            claimant_id="operator",
            expected_revision=decision.revision,
        )
        bundle = ModelFacingControllerActionBundle(
            1,
            decision.decision_id,
            claim.generation,
            "attention-restart-ack",
            claim.revision,
            (),
            (ModelFacingControllerAction(ControllerActionKind.REQUIRE_HUMAN_ATTENTION, reason="follow up"),),
            "restart ack",
        )
        receipt = ledger.submit_controller_actions(bundle, claimant_id="operator", token=str(claim.token))
        harness = _configured_harness(ledger, root, int(_harness(ledger, root)["epoch"]))
        launches: list[tuple[object, bool]] = []
        monkeypatch.setattr(
            harness,
            "_spawn_controller_generation",
            lambda status, *, recovery=False: launches.append((status, recovery)) or True,
        )

        harness._schedule_controller_generations()
        assert len(launches) == 1 and launches[0][1] is True

        ledger.acknowledge_recovered_controller_action(
            decision.decision_id,
            action_id=receipt.action_id,
            bundle_sha256=bundle.sha256,
            committed_revision=receipt.expected_revision + 1,
        )
        launches.clear()
        harness._schedule_controller_generations()
        assert launches == []
        ledger.close()


@pytest.mark.parametrize("restart_before_recovery", [False, True], ids=["same-epoch", "replacement-epoch"])
def test_failed_spawn_commit_fault_is_detectable_and_reconciled_once(
    monkeypatch: pytest.MonkeyPatch,
    restart_before_recovery: bool,
) -> None:
    """A terminalization commit fault retains artifacts and one recovery obligation."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        _prepare_idle_continuation(ledger, root, epoch)
        harness = _configured_harness(ledger, root, epoch)
        commit_fault_armed = False

        def fail_launch(*args: object, **kwargs: object) -> _LiveChild:
            nonlocal commit_fault_armed
            commit_fault_armed = True
            raise OSError("injected launch failure")

        def fail_terminalization_commit(stage: str) -> None:
            if commit_fault_armed and stage == "at_commit_boundary":
                raise LedgerError("injected failed-spawn terminalization commit failure")

        monkeypatch.setattr(harness_module.subprocess, "Popen", fail_launch)
        ledger._fault_injector = fail_terminalization_commit
        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="2000-01-02T00:00:00Z",
        )

        with pytest.raises(FailedSpawnTerminalizationError, match="before durable terminalization"):
            harness._spawn_one(claimed, recovery_continuation=True)

        queue = ledger.queue_dispatch(DISPATCH)
        assert queue["state"] == "starting"
        assert queue["attempt"] == 2
        assert ledger.recovery_state(DISPATCH)["recovery_state"] == "none"
        assert ledger.worker_liveness(DISPATCH) is None
        digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        capability_path = harness.runtime_root / f"capability-{digest}-g1-a2.json"
        capsule_path = harness.runtime_root / f"capsule-{digest}-g1-a2.json"
        assert capability_path.exists()
        assert capsule_path.exists()

        ledger._fault_injector = None
        if restart_before_recovery:
            ledger._db().execute("UPDATE harness_authority SET expires_at = '2000-01-01T00:00:00Z' WHERE singleton = 1")
            ledger._db().commit()
            replacement = ledger.acquire_harness(
                repository_root=root,
                state_root=root,
                pid=os.getpid(),
                process_birth_identity="replacement-after-spawn-fault",
                executable_digest="c" * 64,
                version="test-v12",
                owner_nonce_sha256="d" * 64,
            )
            harness.epoch = int(replacement["epoch"])

        harness.recover_once()
        first_terminal_queue = ledger.queue_dispatch(DISPATCH)
        first_terminal_recovery = ledger.recovery_state(DISPATCH)
        assert first_terminal_queue["state"] == "human_attention_required"
        assert first_terminal_recovery["recovery_state"] == "human_attention_required"
        assert first_terminal_queue["attempt"] == 2
        assert first_terminal_recovery["continuation_used"] == 1
        assert first_terminal_recovery["fresh_thread_used"] == 0
        assert ledger.worker_liveness(DISPATCH) is None
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 1
        assert ledger.queue_dispatch(DISPATCH)["raw_result_json"] is None
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 0
        assert ledger._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0

        harness.recover_once()
        assert ledger.queue_dispatch(DISPATCH) == first_terminal_queue
        assert ledger.recovery_state(DISPATCH) == first_terminal_recovery
        assert ledger._db().execute("SELECT COUNT(*) FROM attempt_capabilities").fetchone()[0] == 1
        assert ledger.queue_dispatch(DISPATCH)["raw_result_json"] is None
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 0
        assert ledger._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
        assert capability_path.exists()
        assert capsule_path.exists()
        ledger.close()


def test_failed_spawn_artifact_cleanup_fault_preserves_durable_terminalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private-file cleanup fault cannot roll back durable queue authority."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _production_queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        _prepare_idle_continuation(ledger, root, epoch)
        harness = _configured_harness(ledger, root, epoch)
        monkeypatch.setattr(
            harness_module.subprocess,
            "Popen",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected launch failure")),
        )
        original_unlink = Path.unlink

        def fail_capsule_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            if path.name.startswith("capsule-"):
                raise OSError("injected artifact cleanup failure")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_capsule_cleanup)
        claimed = ledger.begin_recovery_continuation(
            DISPATCH,
            epoch=epoch,
            claim_nonce_sha256="b" * 64,
            now="2000-01-02T00:00:00Z",
        )

        with pytest.raises(HarnessError, match="worker launch failed"):
            harness._spawn_one(claimed, recovery_continuation=True)

        assert ledger.queue_dispatch(DISPATCH)["state"] == "human_attention_required"
        assert ledger.recovery_state(DISPATCH)["recovery_state"] == "human_attention_required"
        digest = hashlib.sha256(DISPATCH.encode()).hexdigest()[:24]
        assert not harness.runtime_root.joinpath(f"capability-{digest}-g1-a2.json").exists()
        assert harness.runtime_root.joinpath(f"capsule-{digest}-g1-a2.json").exists()
        assert ledger.queue_dispatch(DISPATCH)["raw_result_json"] is None
        assert ledger._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0] == 0
        assert ledger._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
        ledger.close()


def test_generic_recovery_cannot_requeue_pending_or_human_attention_states() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        token = "worker-token"
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="checkpoint-worker",
            lease_token_sha256=token_sha256,
        )
        checkpoint = ledger.claim_due_checkpoint(epoch=epoch, now="9999-01-01T00:00:00Z")
        assert checkpoint is not None
        payload_before = checkpoint["payload_json"]
        ledger.mark_worker_exit(
            DISPATCH,
            pid=os.getpid(),
            process_birth_identity="checkpoint-worker",
        )

        pending_before = ledger.queue_dispatch(DISPATCH)
        recovery_before = ledger.recovery_state(DISPATCH)
        with pytest.raises(StaleWriter, match="typed recovery action"):
            ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        assert ledger.queue_dispatch(DISPATCH) == pending_before
        assert ledger.recovery_state(DISPATCH) == recovery_before
        assert ledger.worker_liveness(DISPATCH)["exited_at"] is not None  # type: ignore[index]
        retained = next(
            item for item in ledger.wake_outbox() if item["dispatch_id"] == DISPATCH and item["kind"] == "checkpoint"
        )
        assert retained["payload_json"] == payload_before

        ledger.mark_human_attention_required(DISPATCH, reason="ambiguous owner")
        attention_before = ledger.queue_dispatch(DISPATCH)
        recovery_attention_before = ledger.recovery_state(DISPATCH)
        with pytest.raises(StaleWriter, match="typed recovery action"):
            ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        assert ledger.queue_dispatch(DISPATCH) == attention_before
        assert ledger.recovery_state(DISPATCH) == recovery_attention_before
        ledger.close()


def test_generic_recovery_cannot_requeue_retry_or_continuation_states() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="retry-worker",
            lease_token_sha256=token_sha256,
        )
        ledger._db().execute("UPDATE dispatch_queue SET thread_id = 'old-thread' WHERE dispatch_id = ?", (DISPATCH,))
        ledger._db().commit()
        ledger.mark_worker_exit(DISPATCH, pid=os.getpid(), process_birth_identity="retry-worker")

        ledger.record_recovery_inspection(
            DISPATCH,
            kind="active_writer",
            thread_id="old-thread",
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        retry_before = ledger.queue_dispatch(DISPATCH)
        with pytest.raises(StaleWriter, match="typed recovery action"):
            ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        assert ledger.queue_dispatch(DISPATCH) == retry_before

        ledger.begin_recovery_inspection(DISPATCH, epoch=epoch, now="2000-01-02T00:00:00Z")
        ledger.record_recovery_inspection(
            DISPATCH,
            kind="idle_no_result",
            thread_id="old-thread",
            next_eligible_at="2000-01-01T00:00:00Z",
        )
        continuation_before = ledger.queue_dispatch(DISPATCH)
        recovery_before = ledger.recovery_state(DISPATCH)
        with pytest.raises(StaleWriter, match="typed recovery action"):
            ledger.recover_queue_dispatch(DISPATCH, new_epoch=epoch)
        assert ledger.queue_dispatch(DISPATCH) == continuation_before
        assert ledger.recovery_state(DISPATCH) == recovery_before
        ledger.close()


@pytest.mark.parametrize(
    ("failure", "exit_code", "diagnostic"),
    [
        (ResponseChainInvalid("invalid chain"), WORKER_EXIT_RESPONSE_CHAIN_INVALID, "response-chain-invalid"),
        (
            TerminalFailureAfterIdentity("terminal defect"),
            WORKER_EXIT_TERMINAL_AFTER_IDENTITY,
            "terminal-after-identity",
        ),
        (
            ResultTransportAfterIdentity("submit transport exhausted"),
            WORKER_EXIT_RESULT_TRANSPORT_AFTER_IDENTITY,
            "result-transport-after-identity",
        ),
    ],
)
def test_worker_main_classifies_terminal_sdk_failures_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    exit_code: int,
    diagnostic: str,
) -> None:
    def fail(**_: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(worker_module, "run_sdk_worker", fail)
    monkeypatch.setattr(
        "sys.argv",
        [
            "codex-flow-worker",
            "--capability-file",
            "cap.json",
            "--capsule-file",
            "capsule.json",
            "--result-file",
            "result.json",
            "--socket-path",
            "harness.sock",
        ],
    )

    assert worker_module.main() == exit_code
    stderr = capsys.readouterr().err
    assert diagnostic in stderr
    assert "Traceback" not in stderr


def test_profile_failure_is_human_attention_configuration_drift_without_malformed_retry() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        authority = _harness(ledger, root)
        epoch = int(authority["epoch"])
        ledger.claim_queue_dispatch(epoch=epoch, claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=epoch)
        token_sha256 = hashlib.sha256(b"worker-token").hexdigest()
        ledger.issue_attempt_capability(
            DISPATCH,
            generation=1,
            attempt=1,
            operation="submit_result",
            schema_sha256=model_facing_result_schema_sha256(),
            workspace_path=root,
            backend="sdk_headless",
            token_sha256=token_sha256,
            expires_at="9999-12-31T23:59:59Z",
        )
        ledger.bind_worker_liveness(
            DISPATCH,
            epoch=epoch,
            generation=1,
            attempt=1,
            pid=os.getpid(),
            process_birth_identity="profile-drift-worker",
            lease_token_sha256=token_sha256,
        )
        ledger.mark_worker_exit(
            DISPATCH,
            pid=os.getpid(),
            process_birth_identity="profile-drift-worker",
            exit_code=83,
            classification="profile-failure",
        )

        queue = ledger.queue_dispatch(DISPATCH)
        recovery = ledger.recovery_state(DISPATCH)
        retry = ledger.retry_policy(DISPATCH)
        assert queue["state"] == "human_attention_required"
        assert recovery["human_attention_reason"] == "native compatibility profile changed before SDK identity"
        assert retry["last_failure"] == "profile"
        assert retry["strategy"] == "human_attention_required"
        assert retry["human_attention_reason"] == "native compatibility profile changed before SDK identity"
        assert queue["raw_result_json"] is None
        ledger.close()


def test_restart_recovers_running_queue_without_migrated_liveness() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        first = _harness(ledger, root)
        ledger.claim_queue_dispatch(epoch=int(first["epoch"]), claim_nonce_sha256="a" * 64)
        ledger.set_queue_state(DISPATCH, "starting", epoch=int(first["epoch"]))
        ledger.set_queue_state(DISPATCH, "running", epoch=int(first["epoch"]))
        ledger._db().execute("UPDATE harness_authority SET expires_at = '2000-01-01T00:00:00Z' WHERE singleton = 1")
        ledger._db().commit()
        second = ledger.acquire_harness(
            repository_root=root,
            state_root=root,
            pid=os.getpid(),
            process_birth_identity="replacement-process",
            executable_digest="c" * 64,
            version="test-v11",
            owner_nonce_sha256="d" * 64,
        )
        harness = object.__new__(WorkflowHarness)
        harness.ledger = ledger
        harness.epoch = int(second["epoch"])

        harness.recover_once()

        recovered = ledger.queue_dispatch(DISPATCH)
        assert recovered["state"] == "human_attention_required"
        assert recovered["attempt"] == 1
        assert ledger.worker_liveness(DISPATCH) is None
        ledger.close()


def test_v11_opener_refuses_to_migrate_under_live_v10_harness() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "workflow.db"
        ledger = _queued_ledger(root)
        _harness(ledger, root)
        ledger.close()
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in ("wake_outbox", "authorized_successors", "worker_liveness", "queue_bindings"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE dispatch_terminal_integrity")
        connection.execute("ALTER TABLE harness_authority RENAME TO supervisor_authority")
        connection.execute(
            "UPDATE supervisor_authority SET process_birth_identity = ?, expires_at = ? WHERE singleton = 1",
            (process_birth_identity(), "9999-12-31T23:59:59.999999Z"),
        )
        connection.execute("UPDATE schema_meta SET value = '10' WHERE key = 'schema_version'")
        connection.execute(
            "UPDATE schema_meta SET value = 'codex_flow_h6e_detached_supervisor_v10' WHERE key = 'schema_identity'"
        )
        connection.commit()
        connection.close()

        with pytest.raises(UnsupportedSchemaVersion, match="live schema-v10 supervisor"):
            Ledger(path, migrate=True)

        check = sqlite3.connect(path)
        assert check.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "10"
        check.close()


def test_v11_marker_with_v12_recovery_residue_fails_closed() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        ledger = _queued_ledger(root)
        ledger._db().execute("DROP TABLE dispatch_terminal_integrity")
        ledger._db().execute("UPDATE schema_meta SET value = '11' WHERE key = 'schema_version'")
        ledger._db().execute(
            "UPDATE schema_meta SET value = 'codex_flow_h6e_harness_wake_v11' WHERE key = 'schema_identity'"
        )
        ledger._db().commit()
        ledger.close()
        with pytest.raises(CorruptSchemaError, match="unexpected v12 recovery state"):
            Ledger(root / "workflow.db", migrate=True)
