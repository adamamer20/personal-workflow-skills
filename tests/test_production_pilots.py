from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import codex_flow.workflow_control_pilot as workflow_control_pilot_module
from codex_flow.cli import app
from codex_flow.harness import WorkflowHarness, process_birth_identity
from codex_flow.ledger import (
    _APP_NATIVE_DISPATCHES_DRAFT_V9_DDL,
    _V8_TABLE_DDL,
    CURRENT_SCHEMA_VERSION,
    Ledger,
    ledger_schema_compatibility,
)
from codex_flow.production_pilots import (
    CompatibilityClassification,
    LegacyRetirementDecision,
    LegacyRetirementDecisionRecord,
    _initialize_production_repository,
    _production_tree_digest,
    make_legacy_decision,
    production_pilot_specs,
    write_production_evidence,
)

INTEGRATED_WHEEL = Path("dist/integrated-runtime/codex_flow-0.2.0-py3-none-any.whl")
INTEGRATED_WHEEL_SHA256 = "93c7256adebf3e2337e7f783407afbf0bb3c34dc5c395139c1951aa0d9daba50"
INTEGRATED_DISPATCHES = (
    "integrated/worker-one/executor/1",
    "integrated/worker-two/executor/1",
)


_INTEGRATED_SERVICE_ORCHESTRATOR = r"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from codex_flow.contracts import (
    ModelFacingControllerAction,
    ModelFacingControllerActionBundle,
    PluginCapabilitySnapshot,
    PluginRequirement,
    model_facing_result_schema_sha256,
)
from codex_flow.backends.codex_sdk import (
    CodexSdkAdapter,
    CodexSdkConfig,
    ControllerThreadInspection,
    ControllerThreadInspectionKind,
)
from codex_flow.control_client import ControlClientError, LiveWorkerControlClient
from codex_flow.controller_recovery import ControllerGenerationRecovery
from codex_flow.domain import (
    ControllerActionKind,
    ControllerClaimantKind,
    NativePermissionAuthority,
    NativePermissionMode,
    ReasoningEffort,
    Sandbox,
    SkillInput,
    ThreadIdentity,
)
from codex_flow.ipc import send_request
from codex_flow.ledger import DIAGNOSTIC_RING_MAX_BYTES, DIAGNOSTIC_RING_MAX_ENTRIES, Ledger, StaleWriter
from codex_flow.plugin_capabilities import (
    PluginCapabilityError,
    PluginCapabilityUnavailable,
    _verified_skill_input,
    bundle_digest,
    discover_plugin_capabilities,
    resolve_plugin_requirements,
)
from codex_flow.service import generate_unit, install_unit
from codex_flow.tui import CodexFlowTerminalApp
from codex_flow.tui_client import TerminalUiClient
from codex_flow.worker import submit_result


DISPATCHES = ("integrated/worker-one/executor/1", "integrated/worker-two/executor/1")
LEAF_POLICY = {
    "agents.enabled": False,
    "features.multi_agent": False,
    "config_overrides": ["agents.enabled=false", "features.multi_agent=false"],
}
AUTHORITY = NativePermissionAuthority(NativePermissionMode.READ_ONLY, "read-only", "never")


def _fake_worker_script(path: Path) -> None:
    path.write_text(
        '''from __future__ import annotations
import hashlib, json, os, sys, time
from pathlib import Path
from codex_flow.ipc import send_request
from codex_flow.worker import submit_result

args = dict(zip(sys.argv[1::2], sys.argv[2::2], strict=True))
capability_path = Path(args["--capability-file"])
result_path = Path(args["--result-file"])
socket_path = Path(args["--socket-path"])
capability = json.loads(capability_path.read_text(encoding="utf-8"))
dispatch_id = str(capability["dispatch_id"])
generation = int(capability["generation"])
attempt = int(capability["attempt"])
token = str(capability["token"])
thread_id = "fake-thread-" + dispatch_id.replace("/", "-")
turn_id = "fake-turn-" + dispatch_id.replace("/", "-")
worker_slug = dispatch_id.replace("/", "-")
provider_calls: list[str] = []
app_api_calls: list[str] = []
controller_model_tokens: list[int] = []
Path(capability["workspace_path"], f"worker-pid-{worker_slug}.txt").write_text(str(os.getpid()), encoding="ascii")
Path(capability["workspace_path"], f"worker-observation-{worker_slug}.json").write_text(
    json.dumps(
        {
            "dispatch_id": dispatch_id,
            "provider_calls": len(provider_calls),
            "app_api_calls": len(app_api_calls),
            "controller_model_tokens": len(controller_model_tokens),
            "skill_input_consumed": False,
            "skill_input_name": None,
            "steer_marker_applied": False,
            "interrupt_seen": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    ),
    encoding="utf-8",
)

# Consume the exact typed plugin authority issued by the production
# harness.  The descriptor is held while the fake SDK turn runs, matching
# the worker's provider boundary without constructing a provider client.
from codex_flow.contracts import PluginCapabilitySnapshot, PluginRequirement
from codex_flow.plugin_capabilities import _verified_skill_input
requirements = tuple(PluginRequirement.from_json(item) for item in capability["plugin_requirements"])
snapshots = tuple(
    PluginCapabilitySnapshot.from_json(
        {key: value for key, value in item.items() if key != "capability_sha256"}
    )
    for item in capability["plugin_capabilities"]
)
plugin_home = Path(os.environ["CODEX_HOME"])
skill_input_consumed = False
skill_input_name = None
with _verified_skill_input(requirements, snapshots, plugin_home) as skill_input:
    if skill_input is not None:
        skill_input_consumed = Path(skill_input.path).is_dir()
        skill_input_name = skill_input.name

def request(operation: str, **fields: object) -> dict[str, object]:
    while True:
        try:
            return send_request(socket_path, {"version": 1, "operation": operation, **fields})
        except (OSError, RuntimeError, ValueError):
            # The service-crash/restart adversary temporarily removes the
            # endpoint; the durable worker remains alive and retries the same
            # capability-bound request after adoption.
            time.sleep(0.05)

request("bind_worker", dispatch_id=dispatch_id, generation=generation, attempt=attempt, token=token, thread_id=thread_id)
request("bind_turn", dispatch_id=dispatch_id, generation=generation, attempt=attempt, token=token, thread_id=thread_id, turn_id=turn_id)
request(
    "worker_event",
    dispatch_id=dispatch_id,
    generation=generation,
    attempt=attempt,
    token=token,
    thread_id=thread_id,
    turn_id=turn_id,
    sequence=1,
    kind="turn/started",
    text="provider-free worker started",
)
# Exercise the diagnostic ring only through the live IPC boundary.  The
# harness owns eviction and byte limits; no post-stop SQLite append is
# permitted by this pilot.
event_sequences = range(2, 131) if dispatch_id == "integrated/worker-one/executor/1" else range(2, 3)
for sequence in event_sequences:
    request(
        "worker_event",
        dispatch_id=dispatch_id,
        generation=generation,
        attempt=attempt,
        token=token,
        thread_id=thread_id,
        turn_id=turn_id,
        sequence=sequence,
        kind="item/completed",
        text=f"event-{sequence}",
    )
steered = False
interrupted = False
while not steered and not interrupted:
    response = request(
        "poll_commands",
        dispatch_id=dispatch_id,
        generation=generation,
        attempt=attempt,
        token=token,
        thread_id=thread_id,
        turn_id=turn_id,
    )
    for command in response.get("commands", []):
        if not isinstance(command, dict):
            continue
        command_id = command.get("command_id")
        kind = command.get("kind")
        if not isinstance(command_id, str):
            continue
        if kind == "steer":
            steered = "INTEGRATED_STEER_MARKER" in str(command.get("payload", ""))
            request(
                "ack_control",
                dispatch_id=dispatch_id,
                generation=generation,
                attempt=attempt,
                token=token,
                command_id=command_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind="steer",
                state="acknowledged",
                detail="steer applied",
            )
        elif kind == "interrupt":
            interrupted = True
            # Interrupt acknowledgement is owned by terminal evidence.  The
            # result is intentionally failed because ModelFacingResult has no
            # interrupted status; WorkflowHarness records the rejected terminal
            # acknowledgement without replaying the command.
    time.sleep(0.02)
summary = "INTEGRATED_STEER_MARKER applied" if steered else "interrupted terminal"
result = {
    "schema_version": 1,
    "status": "failed" if interrupted else "completed",
    "summary": summary,
    "changed_surfaces": [],
    "validations": [
        {"name": "provider_free", "passed": True, "evidence": "typed fake SDK seam"},
        {"name": "typed_skill_input", "passed": skill_input_consumed, "evidence": skill_input_name or "none"},
        {"name": "steer_marker", "passed": steered, "evidence": summary},
    ],
    "durable_status": "failed" if interrupted else "completed",
    "next_action": None,
}
result_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
os.chmod(result_path, 0o400)
# Retain one sanitized observation in the disposable workspace so the pilot
# reads behavior from persisted worker output rather than a test constant.
observation = {
    "dispatch_id": dispatch_id,
    "provider_calls": len(provider_calls),
    "app_api_calls": len(app_api_calls),
    "controller_model_tokens": len(controller_model_tokens),
    "skill_input_consumed": skill_input_consumed,
    "skill_input_name": skill_input_name,
    "steer_marker_applied": steered,
    "interrupt_seen": interrupted,
    "raw_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
}
(Path(capability["workspace_path"]) / f"worker-observation-{dispatch_id.replace('/', '-')}.json").write_text(
    json.dumps(observation, sort_keys=True, separators=(",", ":")), encoding="utf-8"
)
submit_result(capability_file=capability_path, result_file=result_path, socket_path=socket_path)
''',
        encoding="utf-8",
    )
    os.chmod(path, 0o700)


def _queue_case(
    root: Path,
    count: int,
    *,
    plugin_home: Path,
    plugin_requirement: PluginRequirement,
    plugin_snapshot: PluginCapabilitySnapshot,
) -> None:
    if not plugin_home.is_dir():
        raise RuntimeError("strict plugin home is unavailable")
    state = root / ".codex-flow"
    state.mkdir(mode=0o700)
    ledger = Ledger(state / "workflow.db")
    ledger.create_run("integrated")
    route = {
        "effective_permission": AUTHORITY.facts,
        "leaf_worker_policy": LEAF_POLICY,
        "native_compatibility_sha256": None,
        "native_profile_sha256": None,
        "plugin_capabilities": [
            {**plugin_snapshot.to_json(), "capability_sha256": plugin_snapshot.capability_digest}
        ],
        "plugin_requirements": [plugin_requirement.to_json()],
    }
    for index in range(count):
        milestone = ("worker-one", "worker-two")[index]
        dispatch = DISPATCHES[index]
        ledger.create_milestone("integrated", milestone)
        ledger.claim_dispatch("integrated", milestone, "executor", 1)
        ledger.enqueue_dispatch(
            dispatch,
            backend="sdk_headless",
            capsule_json=json.dumps(
                {
                    "model": "gpt-test",
                    "reasoning_effort": "medium",
                    "prompt": "provider-free integrated worker",
                    "workspace_path": os.fspath(root),
                    "plugin_requirements": [plugin_requirement.to_json()],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            route_json=json.dumps(route, sort_keys=True, separators=(",", ":")),
            workspace_path=root,
            result_contract_sha256=model_facing_result_schema_sha256(),
            permission_mode=NativePermissionMode.READ_ONLY,
            effective_permission=AUTHORITY,
            checkpoint_seconds=60.0,
            source_thread_id=f"source-thread-{dispatch.replace('/', '-')}",
        )
    if count == 2:
        ledger.authorize_successor(DISPATCHES[0], DISPATCHES[1])
    ledger.close()


def _plugin_probe(
    parent: Path,
) -> tuple[Path, PluginRequirement, PluginCapabilitySnapshot, dict[str, object]]:
    home = parent / "plugin-home"
    plugin = home / "plugins" / "demo-plugin"
    skill = plugin / "skills" / "demo.skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("provider-free skill\n", encoding="utf-8")
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "id": "demo-plugin",
                "version": "1.0.0",
                "source": "bundled",
                "enabled": True,
                "skills": ["demo.skill"],
                "connectors": ["demo-mcp"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    snapshot = discover_plugin_capabilities(home)[0]
    requirement = PluginRequirement(
        "demo-plugin", "1.0.0", "bundled", bundle_digest(plugin), ("demo.skill",), ()
    )
    resolved = resolve_plugin_requirements((requirement,), home)
    with _verified_skill_input((requirement,), resolved, home) as skill_input:
        skill_bound = skill_input is not None and Path(skill_input.path).is_dir()
    (skill / "SKILL.md").write_text("drift\n", encoding="utf-8")
    try:
        resolve_plugin_requirements((requirement,), home)
    except PluginCapabilityUnavailable:
        drift_rejected = True
    else:
        drift_rejected = False
    # Restore the exact fixture bytes for the live service; the drift case is
    # an isolated negative observation, never queued authority.
    (skill / "SKILL.md").write_text("provider-free skill\n", encoding="utf-8")
    # Keep the service fixture clean; exercise the secret-negative manifest in
    # an isolated sibling home so strict discovery remains usable by workers.
    negative_home = parent / "plugin-negative-home"
    bad = negative_home / "plugins" / "bad-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text(
        '{"id":"bad-plugin","version":"1","source":"bundled","enabled":true,"api_key":"secret"}',
        encoding="utf-8",
    )
    try:
        discover_plugin_capabilities(negative_home)
    except PluginCapabilityError:
        secret_rejected = True
    else:
        secret_rejected = False
    # Incomparable native approval policies are a permission-drift adversary;
    # the typed authority meet operation must reject them before any worker
    # or provider boundary is reached.
    try:
        NativePermissionAuthority(
            NativePermissionMode.INHERIT_NATIVE, "workspace-write", "untrusted"
        ).meet(
            NativePermissionAuthority(
                NativePermissionMode.INHERIT_NATIVE, "workspace-write", "on-request"
            )
        )
    except ValueError:
        permission_drift_rejected = True
    else:
        permission_drift_rejected = False
    return home, requirement, snapshot, {
        "strict_fixture_ready": snapshot.readiness.value == "ready",
        "typed_skill_bound": skill_bound,
        "connector_ids": list(snapshot.mcp_connectors),
        "connector_invocations": 0,
        "plugin_drift_rejected": drift_rejected,
        "permission_drift_rejected": permission_drift_rejected,
        "secret_negative_rejected": secret_rejected,
    }


def _recovery_probe() -> dict[str, object]:
    # Exercise the production controller-recovery API across both loss windows.

    def seed(label: str) -> tuple[Path, Ledger, str, object, object]:
        root = Path(tempfile.mkdtemp(prefix=f"cf-int-recovery-{label}-"))
        state = root / ".codex-flow"
        state.mkdir(mode=0o700)
        ledger = Ledger(state / "workflow.db")
        run = f"recovery-{label}"
        milestone = "source"
        dispatch = f"{run}/{milestone}/executor/1"
        ledger.create_run(run)
        ledger.create_milestone(run, milestone)
        ledger.claim_dispatch(run, milestone, "executor", 1)
        route = {
            "effective_permission": AUTHORITY.facts,
            "leaf_worker_policy": LEAF_POLICY,
            "native_compatibility_sha256": None,
            "native_profile_sha256": None,
            "plugin_capabilities": [],
            "plugin_requirements": [],
        }
        ledger.enqueue_dispatch(
            dispatch,
            backend="sdk_headless",
            capsule_json=json.dumps(
                {"model": "gpt-test", "reasoning_effort": "medium", "prompt": "controller recovery"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            route_json=json.dumps(route, sort_keys=True, separators=(",", ":")),
            workspace_path=root,
            result_contract_sha256=model_facing_result_schema_sha256(),
            permission_mode=NativePermissionMode.READ_ONLY,
            effective_permission=AUTHORITY,
            source_thread_id=f"source-thread-{label}",
        )
        decision = ledger.create_controller_decision(
            dispatch,
            kind="checkpoint",
            source_thread_id=f"source-thread-{label}",
        )
        wake = ledger.claim_wake(f"wake/{dispatch}/checkpoint")
        if wake is None:
            raise RuntimeError("controller recovery wake was not persisted")
        ledger.record_wake_delivery(str(wake["delivery_id"]), outcome="delivered", source_turn_id=f"wake-{label}")
        claim = ledger.claim_controller_decision(
            decision.decision_id,
            claimant_kind=ControllerClaimantKind.MODEL,
            claimant_id=f"source-model-{label}",
            expected_revision=ledger.controller_decision(decision.decision_id).revision,
            lease_seconds=300,
        )
        ledger.prepare_controller_generation(decision.decision_id, generation=1)
        ledger.bind_controller_generation_thread(
            decision.decision_id, generation=1, controller_thread_id=f"controller-thread-{label}"
        )
        ledger.bind_controller_generation_turn(
            decision.decision_id,
            generation=1,
            controller_thread_id=f"controller-thread-{label}",
            controller_turn_id=f"controller-turn-{label}",
        )
        ledger.complete_controller_generation(
            decision.decision_id, generation=1, state="failed"
        )
        return root, ledger, dispatch, decision, claim

    class LedgerRecoveryClient:
        def __init__(self, ledger: Ledger, *, fail_before_commit: bool = False, fail_after_ack: bool = False) -> None:
            self.ledger = ledger
            self.fail_before_commit = fail_before_commit
            self.fail_after_ack = fail_after_ack
            self.submit_calls = 0
            self.ack_calls = 0

        def status(self, decision_id: str):
            return self.ledger.controller_decision(decision_id)

        def generation(self, decision_id: str, generation: int | None = None):
            return self.ledger.controller_generation(decision_id, generation)

        def reserve_recovery_inspection(self, decision_id: str):
            return self.ledger.reserve_controller_recovery_inspection(decision_id)

        def complete_recovery_inspection(self, decision_id: str, *, inspection_outcome: str, claim, bundle=None):
            return self.ledger.complete_controller_recovery_inspection(
                decision_id,
                inspection_outcome=inspection_outcome,
                claim=claim,
                bundle=bundle,
            )

        def submit_recovered_actions(self, decision_id: str):
            self.submit_calls += 1
            if self.fail_before_commit:
                self.fail_before_commit = False
                raise RuntimeError("injected before-decision-commit loss")
            return self.ledger.submit_recovered_controller_actions(decision_id)

        def acknowledge_recovered(self, decision_id: str, *, action_id: str, bundle_sha256: str, committed_revision: int):
            self.ack_calls += 1
            if self.fail_after_ack:
                self.fail_after_ack = False
                raise RuntimeError("injected after-action-commit-before-ack loss")
            receipt = self.ledger.acknowledge_recovered_controller_action(
                decision_id,
                action_id=action_id,
                bundle_sha256=bundle_sha256,
                committed_revision=committed_revision,
            )
            return receipt

    class FakeControllerSdk:
        def __init__(self, bundle: ModelFacingControllerActionBundle) -> None:
            self.bundle = bundle
            self.reads = 0

        def inspect_controller_thread(self, thread: ThreadIdentity, *, decision_id, generation):
            self.reads += 1
            return ControllerThreadInspection(
                thread.id,
                int(generation),
                ControllerThreadInspectionKind.COMPLETED,
                turn_id=f"inspected-{generation}",
                bundle=self.bundle,
            )

    def run_window(label: str, *, after_commit: bool) -> dict[str, object]:
        root, ledger, dispatch, decision, claim = seed(label)
        bundle = ModelFacingControllerActionBundle(
            1,
            decision.decision_id,
            claim.generation,
            f"recovered-action-{label}",
            claim.revision,
            (),
            (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
            f"recovered {label}",
        )
        adapter = FakeControllerSdk(bundle)
        first_client = LedgerRecoveryClient(ledger, fail_before_commit=not after_commit, fail_after_ack=after_commit)
        first = ControllerGenerationRecovery(first_client, adapter)
        first_error = None
        try:
            first.recover(str(decision.decision_id))
        except RuntimeError as exc:
            first_error = type(exc).__name__
        ledger.close()
        reopened = Ledger(root / ".codex-flow" / "workflow.db")
        second_client = LedgerRecoveryClient(reopened)
        recovered = ControllerGenerationRecovery(second_client, adapter).recover(str(decision.decision_id))
        decision_row = reopened.controller_decision(decision.decision_id)
        outbox_rows = int(
            reopened._db()
            .execute("SELECT COUNT(*) FROM controller_action_outbox WHERE decision_id = ?", (str(decision.decision_id),))
            .fetchone()[0]
        )
        wake_rows = int(
            reopened._db()
            .execute("SELECT COUNT(*) FROM wake_outbox WHERE decision_id = ? AND source_thread_id IS NOT NULL", (str(decision.decision_id),))
            .fetchone()[0]
        )
        successor_rows = int(reopened._db().execute("SELECT COUNT(*) FROM successor_outbox").fetchone()[0])
        notification_rows = int(reopened._db().execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0])
        table_counts = {
            table: int(reopened._db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("controller_decisions", "controller_decision_generations", "controller_action_outbox")
        }
        reopened.close()
        return {
            "window": label,
            "fault_injected": first_error is not None,
            "recovered_outcome": recovered.outcome,
            "adapter_inspections": adapter.reads,
            "submit_calls": first_client.submit_calls + second_client.submit_calls,
            "ack_calls": first_client.ack_calls + second_client.ack_calls,
            "action_outbox_rows": outbox_rows,
            "decision_state": decision_row.state.value,
            "source_thread_wake_rows": wake_rows,
            "successor_outbox_rows": successor_rows,
            "notification_outbox_rows": notification_rows,
            "worker_application_count": 0,
            "table_counts": table_counts,
            "duplicate_action": outbox_rows != 1,
            "first_loss": first_error,
            "dispatch_id": dispatch,
            "decision_id": str(decision.decision_id),
        }

    before = run_window("before-decision-commit", after_commit=False)
    after = run_window("after-action-commit-before-ack", after_commit=True)

    # A conflicting/stale inspected identity is rejected by the same typed
    # recovered-action API; ambiguous inspection remains human attention only.
    root, ledger, dispatch, decision, claim = seed("ambiguous")
    adapter = FakeControllerSdk(
        ModelFacingControllerActionBundle(
            1,
            decision.decision_id,
            claim.generation,
            "ambiguous-action",
            claim.revision,
            (),
            (ModelFacingControllerAction(ControllerActionKind.ACKNOWLEDGE_ONLY),),
            "ambiguous",
        )
    )
    client = LedgerRecoveryClient(ledger)
    inspection = client.reserve_recovery_inspection(str(decision.decision_id))
    client.complete_recovery_inspection(
        str(decision.decision_id), inspection_outcome="ambiguous", claim=inspection
    )
    try:
        client.submit_recovered_actions(str(decision.decision_id))
    except StaleWriter:
        stale_rejected = True
    else:
        stale_rejected = False
    ambiguous_state = ledger.controller_decision(decision.decision_id).state.value
    table_counts = {
        table: int(ledger._db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("controller_decisions", "controller_decision_generations", "controller_action_outbox")
    }
    ledger.close()
    return {
        "before_decision_commit": before,
        "after_action_commit_before_ack": after,
        "stale_identity_rejected": stale_rejected,
        "ambiguous_state": ambiguous_state,
        "ambiguous_table_counts": table_counts,
        "exactly_one_recovery_action_each": before["action_outbox_rows"] == 1 and after["action_outbox_rows"] == 1,
        "duplicate_action_or_notification": bool(before["duplicate_action"] or after["duplicate_action"]),
        "adapter_inspections": int(adapter.reads),
        "provider_calls": 0,
    }


def _write_service_entrypoint(path: Path, fake_worker: Path, *, lease_seconds: float = 30.0) -> None:
    # Create a disposable executable that invokes the retained wheel CLI.

    path.write_text(
        f'''#!{sys.executable}
from __future__ import annotations
import sys
from codex_flow import cli
from codex_flow.harness import WorkflowHarness as ProductionWorkflowHarness

class IntegratedWorkflowHarness(ProductionWorkflowHarness):
    # This is the production WorkflowHarness and CLI entrypoint; only the worker
    # and controller commands are injected so provider-free behavior can be
    # observed safely without accidentally starting a model turn.
    def __init__(self, state_root, *args, **kwargs):
        kwargs["worker_command"] = (sys.executable, {os.fspath(fake_worker)!r})
        kwargs["controller_command"] = ("provider-must-not-run",)
        kwargs["lease_seconds"] = {lease_seconds!r}
        kwargs["wake_delivery"] = lambda source_thread_id, payload: "fake-wake-turn-" + source_thread_id
        super().__init__(state_root, *args, **kwargs)

cli.WorkflowHarness = IntegratedWorkflowHarness
raise SystemExit(cli.app())
''',
        encoding="utf-8",
    )
    os.chmod(path, 0o700)


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _run_case(
    parent: Path,
    count: int,
    *,
    wheel_site: Path,
    plugin_home: Path,
    plugin_requirement: PluginRequirement,
    plugin_snapshot: PluginCapabilitySnapshot,
    fake_worker: Path,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"cf-int-{count}-") as directory:
        root = Path(directory)
        (root / "README.md").write_text("provider-free integrated case\n", encoding="utf-8")
        _queue_case(
            root,
            count,
            plugin_home=plugin_home,
            plugin_requirement=plugin_requirement,
            plugin_snapshot=plugin_snapshot,
        )
        service_executable = root / "codex-flow-service"
        _write_service_entrypoint(
            service_executable,
            fake_worker,
            lease_seconds=30.0,
        )
        unit = generate_unit(root, executable=service_executable)
        installed_unit = install_unit(unit, config_home=root / "service-config")
        unit_installed = installed_unit.read_text(encoding="utf-8") == unit.text
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.fspath(wheel_site)
        environment["CODEX_HOME"] = os.fspath(plugin_home)
        service_command = (
            os.fspath(service_executable),
            "harness",
            "run",
            "--foreground",
            "--state-root",
            os.fspath(root),
        )
        service = subprocess.Popen(
            service_command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        service_pids = [service.pid]
        client = LiveWorkerControlClient.for_state_root(root, timeout=10.0)
        actions: dict[str, str] = {}
        identities: dict[str, tuple[int, int, str, str]] = {}
        command_results: dict[str, dict[str, object]] = {}
        replay_rejected = False
        service_crash_restart = False
        worker_pid_before_crash: int | None = None
        worker_pid_after_restart: int | None = None
        restart_authority_pid_match = False
        ring_snapshot: dict[str, object] = {}
        activity_counts: dict[str, int] = {}
        activity_first_at: dict[str, str] = {}
        authority_pid_match = False
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            try:
                statuses = client.status()
            except (ControlClientError, OSError, ValueError):
                if service.poll() is not None:
                    stderr = service.stderr.read() if service.stderr is not None else ""
                    raise RuntimeError(f"count={count} retained-wheel service exited early: {stderr}")
                time.sleep(0.03)
                continue
            if not authority_pid_match:
                probe_ledger = Ledger(root / ".codex-flow" / "workflow.db")
                authority = probe_ledger.harness_authority()
                if authority is not None:
                    authority_pid_match = int(authority["pid"]) == service.pid
                probe_ledger.close()
            for status in statuses:
                dispatch = str(status.dispatch_id)
                if status.active_turn_id is None or dispatch in actions:
                    continue
                assert status.thread_id is not None
                identities[dispatch] = (
                    int(status.generation),
                    int(status.attempt),
                    status.thread_id.id,
                    status.active_turn_id,
                )
                if count == 1 and not service_crash_restart:
                    # Kill the service while its worker is active; the worker
                    # remains the durable child and is adopted by the restart.
                    pid_path = root / f"worker-pid-{dispatch.replace('/', '-')}.txt"
                    if pid_path.exists():
                        worker_pid_before_crash = int(pid_path.read_text())
                    service.kill()
                    service.wait(timeout=5)
                    expired_ledger = Ledger(root / ".codex-flow" / "workflow.db")
                    expired_ledger._db().execute(
                        "UPDATE harness_authority SET expires_at = '2000-01-01T00:00:00Z' WHERE singleton = 1"
                    )
                    expired_ledger._db().commit()
                    expired_ledger.close()
                    service = subprocess.Popen(
                        service_command,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    service_pids.append(service.pid)
                    service_crash_restart = True
                    time.sleep(0.15)
                    continue
                if service_crash_restart and not restart_authority_pid_match:
                    restart_ledger = Ledger(root / ".codex-flow" / "workflow.db")
                    restart_authority = restart_ledger.harness_authority()
                    if restart_authority is not None:
                        restart_authority_pid_match = int(restart_authority["pid"]) == service.pid
                    restart_ledger.close()
                    pid_path = root / f"worker-pid-{dispatch.replace('/', '-')}.txt"
                    if pid_path.exists():
                        worker_pid_after_restart = int(pid_path.read_text())
                try:
                    if dispatch == DISPATCHES[0] and count:
                        command = client.steer(
                            dispatch,
                            generation=identities[dispatch][0],
                            attempt=identities[dispatch][1],
                            thread_id=identities[dispatch][2],
                            turn_id=identities[dispatch][3],
                            text="INTEGRATED_STEER_MARKER",
                            command_id=f"integrated-steer-{count}",
                        )
                        actions[dispatch] = "steer"
                        command_results[dispatch] = command.to_json()
                    elif dispatch == DISPATCHES[1] and count == 2:
                        command = client.interrupt(
                            dispatch,
                            generation=identities[dispatch][0],
                            attempt=identities[dispatch][1],
                            thread_id=identities[dispatch][2],
                            turn_id=identities[dispatch][3],
                            command_id="integrated-interrupt-two",
                        )
                        actions[dispatch] = "interrupt"
                        command_results[dispatch] = command.to_json()
                except ControlClientError:
                    continue
            if count == 0:
                break
            if len(actions) == count:
                try:
                    terminal_statuses = client.status()
                    if all(item.state in {"completed", "failed", "human_attention_required"} for item in terminal_statuses):
                        break
                except (ControlClientError, OSError, ValueError):
                    pass
            time.sleep(0.03)

        if count == 2 and DISPATCHES[1] in identities:
            try:
                client.interrupt(
                    DISPATCHES[1],
                    generation=identities[DISPATCHES[1]][0],
                    attempt=identities[DISPATCHES[1]][1],
                    thread_id=identities[DISPATCHES[1]][2],
                    turn_id=identities[DISPATCHES[1]][3],
                    command_id="integrated-interrupt-replay-after-terminal",
                )
            except ControlClientError:
                replay_rejected = True

        # Read the bounded ring over live IPC before stopping the service.
        for dispatch in DISPATCHES[:count]:
            activity = None
            activity_deadline = time.monotonic() + 30.0
            while activity is None and time.monotonic() < activity_deadline:
                try:
                    activity = client.recent_activity(dispatch, limit=128)
                except ControlClientError:
                    if service.poll() is not None:
                        service_stderr = service.stderr.read() if service.stderr is not None else ""
                        raise RuntimeError(
                            f"retained-wheel service exited before activity read for {dispatch}: {service_stderr}"
                        )
                    time.sleep(0.05)
            if activity is None:
                raise RuntimeError(f"timed out reading live activity for {dispatch}")
            entries = tuple(activity.events)
            activity_counts[dispatch] = len(entries)
            if entries:
                activity_first_at[dispatch] = entries[0].occurred_at
            if dispatch == DISPATCHES[0]:
                ring_snapshot = {
                    "entries": len(entries),
                    "payload_bytes": sum(len((event.text or "").encode("utf-8")) for event in entries),
                    "max_entries": DIAGNOSTIC_RING_MAX_ENTRIES,
                    "max_bytes": DIAGNOSTIC_RING_MAX_BYTES,
                }

        tui_client = TerminalUiClient.for_state_root(root)
        snapshot = asyncio.run(tui_client.refresh())
        tui_headless = False
        try:
            async def render_tui() -> None:
                nonlocal tui_headless
                terminal = CodexFlowTerminalApp(tui_client, refresh_on_mount=False, no_color=True)
                async with terminal.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    terminal._render_snapshot(snapshot)
                    await pilot.pause()
                    tui_headless = True

            asyncio.run(render_tui())
        except Exception:
            tui_headless = False

        # Stop through the production harness IPC shutdown operation so its
        # own close path removes the socket and retains a requested-shutdown
        # audit rather than force-killing the lifecycle owner.
        send_request(root / ".codex-flow" / "runtime" / "harness.sock", {"version": 1, "operation": "shutdown"})
        service.wait(timeout=10)
        for process in (service,):
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
        service_stderr = service.stderr.read() if service.stderr is not None else ""
        if service.returncode not in (0, 130, -signal.SIGINT):
            raise RuntimeError(f"retained-wheel service shutdown failed: {service_stderr}")

        ledger = Ledger(root / ".codex-flow" / "workflow.db")
        rows = {str(item["dispatch_id"]): item for item in ledger.queue_dispatches()}
        command_rows = {dispatch: ledger.control_commands(dispatch) for dispatch in rows}
        successor_rows = ledger.authorized_successors(DISPATCHES[0]) if count == 2 else ()
        outbox = ledger._db().execute(
            "SELECT source_dispatch_id, successor_dispatch_id, outcome FROM successor_outbox ORDER BY source_dispatch_id"
        ).fetchall()
        notifications = ledger._db().execute(
            "SELECT dispatch_id, source_task, outcome FROM notification_outbox ORDER BY dispatch_id"
        ).fetchall()
        dependency_order = [
            dispatch
            for dispatch, _occurred_at in sorted(activity_first_at.items(), key=lambda item: item[1])
        ]
        observations: list[dict[str, object]] = []
        worker_pids_gone = True
        for dispatch in DISPATCHES[:count]:
            observation_path = root / f"worker-observation-{dispatch.replace('/', '-')}.json"
            if not observation_path.exists():
                raise RuntimeError(f"worker observation missing for {dispatch}; service stderr={service_stderr}")
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            observations.append(observation)
            pid_path = root / f"worker-pid-{dispatch.replace('/', '-')}.txt"
            if pid_path.exists():
                worker_pids_gone = worker_pids_gone and _pid_is_gone(int(pid_path.read_text()))
        authority = ledger.harness_authority()
        # The production close path intentionally leaves a bounded lease row
        # for stale-owner reconciliation.  Prove there is no live lease owner
        # after shutdown rather than waiting for the full lease duration.
        authority_released = authority is None or (
            int(authority["requested_shutdown"]) == 1 and _pid_is_gone(int(authority["pid"]))
        )
        tui_worker_identity_match = all(
            (
                worker.dispatch_id in identities
                and worker.thread_id == identities[worker.dispatch_id][2]
                and worker.turn_id in {None, identities[worker.dispatch_id][3]}
            )
            for worker in snapshot.workers
        ) and len(snapshot.workers) == len(identities)
        tui_control_identity_match = all(
            str(command["dispatch_id"]) in identities
            and int(command["generation"]) == identities[str(command["dispatch_id"])][0]
            and int(command["attempt"]) == identities[str(command["dispatch_id"])][1]
            and str(command["thread_id"]) == identities[str(command["dispatch_id"])][2]
            and str(command["turn_id"]) == identities[str(command["dispatch_id"])][3]
            for commands in command_rows.values()
            for command in commands
        )
        provider_free = {
            "provider_calls": sum(1 for event in observations if event.get("provider_calls", 0) != 0),
            "app_api_calls": sum(1 for event in observations if event.get("app_api_calls", 0) != 0),
            "controller_model_tokens": sum(int(event.get("controller_model_tokens", 0)) for event in observations),
            "thread_started": len(observations),
            "terminal_states": {dispatch: row["state"] for dispatch, row in rows.items()},
            "activity_events": sum(activity_counts.get(dispatch, 0) for dispatch in rows),
            "commands": {
                dispatch: [{"kind": item["kind"], "state": item["state"]} for item in values]
                for dispatch, values in command_rows.items()
            },
            "actions": actions,
            "command_acknowledgements": {
                dispatch: (values[-1] if values else {}) for dispatch, values in command_rows.items()
            },
            "notification_outbox_rows": len(notifications),
            "notification_source_tasks": [str(item[1]) for item in notifications],
            "zero_duplicate_notifications": len(notifications) == len({str(item[0]) for item in notifications}),
            "dependency_order": dependency_order,
            "dependency_order_measured_from_events": count != 2 or dependency_order == list(DISPATCHES),
            "released_successors": sum(item["released_at"] is not None for item in successor_rows),
            "successor_outbox_rows": len(outbox),
            "zero_duplicate_successor": (
                sum(item[0] == DISPATCHES[0] and item[1] == DISPATCHES[1] for item in outbox) == 1
                if count == 2
                else all(item[1] is None for item in outbox)
            ),
            "socket_removed": not (root / ".codex-flow" / "runtime" / "harness.sock").exists(),
            "worker_processes_stopped": worker_pids_gone,
            "service_crash_restart": service_crash_restart if count == 1 else False,
            "service_processes_stopped": all(_pid_is_gone(pid) for pid in service_pids),
            "service_lease_released": authority_released,
            "service_authority_pid_match": authority_pid_match,
            "restart_authority_pid_match": restart_authority_pid_match if count == 1 else True,
            "worker_adopted_across_crash": (
                worker_pid_before_crash is not None
                and worker_pid_after_restart == worker_pid_before_crash
                if count == 1
                else True
            ),
            "restart_clean": all(_pid_is_gone(pid) for pid in service_pids) and worker_pids_gone,
            "service_unit_installed": unit_installed,
            "service_entrypoint": "codex_flow.cli.harness_run",
            "ring_entries_bounded": bool(ring_snapshot)
            and int(ring_snapshot["entries"]) <= int(ring_snapshot["max_entries"])
            and int(ring_snapshot["payload_bytes"]) <= int(ring_snapshot["max_bytes"]),
            "ring_snapshot": ring_snapshot,
            "steer_observation": next(
                (item for item in observations if item.get("dispatch_id") == DISPATCHES[0]), {}
            ),
            "interrupt_replay_rejected": replay_rejected,
            "tui_snapshot_connected": snapshot.connected,
            "tui_worker_ids": sorted(item.dispatch_id for item in snapshot.workers),
            "tui_headless": tui_headless,
            "tui_worker_identity_match": tui_worker_identity_match,
            "tui_control_identity_match": tui_control_identity_match,
            "tui_decision_ids": [item.decision_id for item in snapshot.decisions],
        }
        ledger.close()
        return provider_free


def _sdk_probe() -> dict[str, object]:
    # Exercise CodexSdkAdapter through its typed provider-free fake seam.

    from types import SimpleNamespace

    class FakeTurn:
        def __init__(self) -> None:
            self.id = "typed-turn-1"

        def stream(self):
            return [
                SimpleNamespace(method="turn/started", payload=SimpleNamespace(turn=SimpleNamespace(id=self.id))),
                SimpleNamespace(
                    method="item/completed",
                    payload=SimpleNamespace(
                        item=SimpleNamespace(root=SimpleNamespace(text='{"ok":true}')),
                        turn_id=self.id,
                    ),
                ),
                SimpleNamespace(
                    method="turn/completed",
                    payload=SimpleNamespace(
                        turn=SimpleNamespace(id=self.id, status=SimpleNamespace(value="completed"), error=None)
                    ),
                ),
            ]

    class FakeThread:
        def __init__(self) -> None:
            self.id = "typed-thread-1"
            self.turn_inputs: list[object] = []

        def turn(self, value: object, **kwargs: object) -> FakeTurn:
            self.turn_inputs.append(value)
            return FakeTurn()

    class FakeClient:
        def __init__(self) -> None:
            self.thread = FakeThread()
            self.thread_start_calls = 0
            self.provider_calls: list[str] = []

        def thread_start(self, **kwargs: object) -> FakeThread:
            self.thread_start_calls += 1
            return self.thread

        def close(self) -> None:
            return None

    class FakeSdk:
        version = "provider-free-fake-sdk"
        Sandbox = SimpleNamespace(read_only="read-only", workspace_write="workspace-write", full_access="full-access")
        ApprovalMode = SimpleNamespace(deny_all="deny-all")
        ReasoningEffort = SimpleNamespace(none="none", minimal="minimal", low="low", medium="medium", high="high", xhigh="xhigh")

        class SkillInput:
            def __init__(self, *, name: str, path: str) -> None:
                self.name = name
                self.path = path

    client = FakeClient()
    adapter = CodexSdkAdapter(
        CodexSdkConfig(
            model="gpt-test",
            reasoning_effort=ReasoningEffort.MEDIUM,
            sandbox=Sandbox.READ_ONLY,
            cwd=Path.cwd(),
        ),
        client_factory=lambda: client,
        sdk=FakeSdk(),
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    thread = adapter.start_thread()
    input_value = SkillInput("demo.skill", "/tmp/demo")
    observation = adapter.run_turn(thread, input_value, output_schema=schema)
    adapter.close()
    consumed = client.thread.turn_inputs[0]
    return {
        "provider_calls": len(client.provider_calls),
        "adapter_start_calls": client.thread_start_calls,
        "typed_input_consumed": isinstance(consumed, FakeSdk.SkillInput)
        and consumed.name == input_value.name
        and consumed.path == input_value.path,
        "structured_output_unchanged": observation.structured_output == {"ok": True},
        "structured_output": observation.structured_output,
        "turn_id": observation.turn_id,
    }


def main() -> None:
    parent = Path(sys.argv[1]).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    fake_worker = parent / "fake-worker.py"
    _fake_worker_script(fake_worker)
    plugin_home, plugin_requirement, plugin_snapshot, plugin = _plugin_probe(parent)
    wheel_path_value = os.environ.get("PYTHONPATH", "")
    wheel_site = Path(wheel_path_value.split(os.pathsep, 1)[0]).resolve() if wheel_path_value else Path.cwd()
    if not (wheel_site / "codex_flow" / "cli.py").is_file():
        raise RuntimeError("exact retained wheel import root is unavailable")
    results = {
        str(count): _run_case(
            parent,
            count,
            wheel_site=wheel_site,
            plugin_home=plugin_home,
            plugin_requirement=plugin_requirement,
            plugin_snapshot=plugin_snapshot,
            fake_worker=fake_worker,
        )
        for count in (0, 1, 2)
    }
    recovery = _recovery_probe()
    sdk = _sdk_probe()
    print(
        json.dumps(
            {
                "matrix": results,
                "plugin": plugin,
                "recovery": recovery,
                "sdk": sdk,
                "service": {
                    "provider_calls": sum(int(item["provider_calls"]) for item in results.values()),
                    "app_api_calls": sum(int(item["app_api_calls"]) for item in results.values()),
                    "controller_model_tokens": sum(int(item["controller_model_tokens"]) for item in results.values()),
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
"""


def test_retirement_decision_defaults_to_retention_without_full_parity() -> None:
    decision = make_legacy_decision(pilots_passed=True, required_behavioral_parity_proven=False)

    assert decision.decision is LegacyRetirementDecision.RETAIN_LEGACY
    assert decision.closed is True
    assert decision.production_reachability_proven is True
    assert decision.required_behavioral_parity_proven is False
    assert decision.cleanup_authorized is False


def test_disablement_decision_fails_closed_without_reachability_and_parity() -> None:
    with pytest.raises(ValueError, match="reachability and behavioral parity"):
        LegacyRetirementDecisionRecord(
            1,
            LegacyRetirementDecision.DISABLE_HOOKS_KEEP_MANUAL,
            True,
            True,
            False,
            ("pilot",),
            ("parity-open",),
        )


def test_ready_decision_is_evidence_only_even_after_full_parity() -> None:
    decision = make_legacy_decision(pilots_passed=True, required_behavioral_parity_proven=True)

    assert decision.decision is LegacyRetirementDecision.READY_FOR_SEPARATE_CLEANUP
    assert decision.cleanup_authorized is False


def test_fixed_capsules_cover_medium_and_large_objective_architecture_work() -> None:
    medium, large = production_pilot_specs()

    assert medium.scale == "medium"
    assert large.scale == "large"
    assert medium.run_id != large.run_id
    assert medium.expected_changed_paths == ("src/text_normalization/slug.py",)
    assert len(large.expected_changed_paths) == 3
    assert len(medium.prompt.encode("utf-8")) < len(large.prompt.encode("utf-8")) < 12_000


@pytest.mark.parametrize("index", (0, 1))
def test_disposable_pilot_repositories_start_real_and_validation_fails_before_execution(
    tmp_path: Path, index: int
) -> None:
    spec = production_pilot_specs()[index]
    root = tmp_path / spec.scale

    base = _initialize_production_repository(root, spec)
    validation = subprocess.run(spec.validation_argv, cwd=root, text=True, capture_output=True, check=False)

    assert len(base) == 40
    assert validation.returncode != 0
    assert subprocess.run(("git", "status", "--short"), cwd=root, capture_output=True, check=True).stdout == b""


def test_tree_digest_ignores_controller_state_but_observes_repository_outcome(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "source.py").write_text("before\n")
    before = _production_tree_digest(root)
    (root / ".codex-flow").mkdir()
    (root / ".codex-flow/workflow.db").write_bytes(b"state")

    assert _production_tree_digest(root) == before
    (root / "source.py").write_text("after\n")
    assert _production_tree_digest(root) != before


def test_production_cli_requires_explicit_real_sdk_authority(tmp_path: Path) -> None:
    config = tmp_path / "workflow.toml"
    config.write_text("placeholder\n")
    result = CliRunner().invoke(
        app,
        ["production-pilots", "--config", str(config), "--output", str(tmp_path / "evidence.json")],
        env={"CODEX_FLOW_REAL_SDK": "0"},
    )

    assert result.exit_code == 2
    assert "refusing real SDK start" in result.stdout


def test_retained_production_evidence_is_closed_json_and_uses_exact_capability_labels(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    evidence = {
        "status": "passed",
        "compatibility": {
            name: {"status": classification.value}
            for name, classification in {
                "local_sdk": CompatibilityClassification.PROVEN,
                "desktop": CompatibilityClassification.NOT_EXPOSED,
                "idle": CompatibilityClassification.NOT_RUN,
                "remote": CompatibilityClassification.UNSUPPORTED,
            }.items()
        },
    }

    write_production_evidence(output, evidence)
    assert json.loads(output.read_text()) == evidence
    assert output.read_bytes().endswith(b"\n")


def test_visible_worker_generator_plans_canonical_capsule_reachable_by_control(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "codex-flow@example.test"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Codex Flow Pilot Test"), cwd=root, check=True)
    (root / "AGENTS.md").write_text("protected\n")
    (root / "workflow.toml").write_text((Path(__file__).parents[1] / "workflow.toml").read_text())
    reviews = root / "docs" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "peer-thread-workflow.md").write_text("protected plan\n")
    (reviews / "codex-controller-compatibility.md").write_text("compatibility\n")
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=root, check=True)
    runner = CliRunner()

    generated = runner.invoke(
        app,
        ["visible-worker-capsule", "--parent-run-id", "parent-run", "--state-root", str(root)],
    )

    assert generated.exit_code == 0, generated.stdout
    capsule_path = Path(generated.stdout.strip())
    assert capsule_path == root / ".codex-flow" / "capsules" / "parent-run" / "h6-c-visible-app-pilot.json"
    assert b"\n  " not in capsule_path.read_bytes()
    prepared = runner.invoke(
        app,
        ["control", "--hosting", "app-native", "--capsule", str(capsule_path), "--state-root", str(root), "--json"],
    )
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.stdout)["state"] == "prepared"


def test_schema_v8_compatibility_probe_is_read_only_before_candidate_migration(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    connection = sqlite3.connect(path)
    for ddl in _V8_TABLE_DDL.values():
        connection.execute(ddl)
    connection.executemany(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", "8"),
            ("migration_marker", "complete"),
            ("schema_identity", "codex_flow_h3_terminal_workspace_v8"),
        ),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    compatibility = ledger_schema_compatibility(path)

    assert compatibility == {
        "candidate_schema_version": int(CURRENT_SCHEMA_VERSION),
        "ledger_schema_version": 8,
        "compatible": True,
        "migration_required": True,
    }
    assert path.read_bytes() == before


def test_empty_source_draft_v9_is_detected_read_only_then_upgraded_by_candidate(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    connection = sqlite3.connect(path)
    for ddl in (*_V8_TABLE_DDL.values(), _APP_NATIVE_DISPATCHES_DRAFT_V9_DDL):
        connection.execute(ddl)
    connection.executemany(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", "9"),
            ("migration_marker", "complete"),
            ("schema_identity", "codex_flow_h6_app_native_v9"),
        ),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    assert ledger_schema_compatibility(path)["migration_required"] is True
    assert path.read_bytes() == before
    ledger = Ledger(path, migrate=True)
    assert ledger_schema_compatibility(path)["migration_required"] is False
    ledger.close()


def test_integrated_control_runs_exact_retained_wheel_service_provider_free(tmp_path: Path) -> None:
    """Exercise one installed-wheel service through the real harness IPC seam.

    The worker process is deliberately a typed fake: it consumes the exact
    capability/result files and sends bind, activity, control and submission
    frames through production IPC, but never constructs the provider SDK.
    This keeps the integrated mechanism proof deterministic while the real
    bundled-plugin/provider sentinel remains an explicit external gate.
    """

    wheel = INTEGRATED_WHEEL.resolve()
    assert wheel.is_file()
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == INTEGRATED_WHEEL_SHA256
    site = tmp_path / "wheel-site"
    site.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    orchestrator = tmp_path / "integrated-service.py"
    orchestrator.write_text(_INTEGRATED_SERVICE_ORCHESTRATOR, encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.fspath(site)
    process = subprocess.Popen(
        (sys.executable, str(orchestrator), str(tmp_path / "service-root")),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise
    completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed["service"] == {"provider_calls": 0, "app_api_calls": 0, "controller_model_tokens": 0}
    assert observed["plugin"] == {
        "strict_fixture_ready": True,
        "typed_skill_bound": True,
        "connector_ids": ["demo-mcp"],
        "connector_invocations": 0,
        "plugin_drift_rejected": True,
        "permission_drift_rejected": True,
        "secret_negative_rejected": True,
    }
    recovery = observed["recovery"]
    assert recovery["stale_identity_rejected"] is True
    assert recovery["ambiguous_state"] == "human_attention_required"
    assert recovery["exactly_one_recovery_action_each"] is True
    assert recovery["duplicate_action_or_notification"] is False
    for window_name in ("before_decision_commit", "after_action_commit_before_ack"):
        window = recovery[window_name]
        assert window["fault_injected"] is True
        assert window["decision_id"] == f"decision/wake/{window['dispatch_id']}/checkpoint"
        assert window["recovered_outcome"] == "acknowledged"
        assert window["adapter_inspections"] == 1
        assert window["action_outbox_rows"] == 1
        assert window["decision_state"] == "acknowledged"
        assert window["source_thread_wake_rows"] == 1
        assert window["successor_outbox_rows"] == 0
        assert window["notification_outbox_rows"] == 0
        assert window["worker_application_count"] == 0
        assert window["duplicate_action"] is False
    matrix = observed["matrix"]
    assert matrix["0"]["thread_started"] == 0
    assert matrix["0"]["terminal_states"] == {}
    assert matrix["1"]["actions"] == {INTEGRATED_DISPATCHES[0]: "steer"}, completed.stderr
    assert matrix["1"]["terminal_states"][INTEGRATED_DISPATCHES[0]] == "completed"
    assert matrix["2"]["actions"] == {
        INTEGRATED_DISPATCHES[0]: "steer",
        INTEGRATED_DISPATCHES[1]: "interrupt",
    }
    assert matrix["2"]["terminal_states"] == {
        INTEGRATED_DISPATCHES[0]: "completed",
        INTEGRATED_DISPATCHES[1]: "failed",
    }
    assert matrix["2"]["released_successors"] == 1
    assert matrix["2"]["successor_outbox_rows"] == 2
    assert matrix["2"]["zero_duplicate_successor"] is True
    assert all(matrix[str(count)]["zero_duplicate_successor"] for count in (0, 1, 2))
    assert matrix["2"]["dependency_order"] == list(INTEGRATED_DISPATCHES)
    assert matrix["2"]["dependency_order_measured_from_events"] is True
    assert matrix["2"]["notification_outbox_rows"] == 2
    assert matrix["1"]["notification_source_tasks"] == ["source-thread-integrated-worker-one-executor-1"]
    assert matrix["2"]["notification_source_tasks"] == [
        "source-thread-integrated-worker-one-executor-1",
        "source-thread-integrated-worker-two-executor-1",
    ]
    assert matrix["2"]["zero_duplicate_notifications"] is True
    assert all(matrix[str(count)]["service_unit_installed"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["socket_removed"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["worker_processes_stopped"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["restart_clean"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["service_lease_released"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["ring_entries_bounded"] for count in (1, 2))
    assert matrix["1"]["service_crash_restart"] is True
    assert matrix["2"]["service_crash_restart"] is False
    assert matrix["1"]["restart_authority_pid_match"] is True
    assert matrix["1"]["worker_adopted_across_crash"] is True
    assert all(matrix[str(count)]["service_entrypoint"] == "codex_flow.cli.harness_run" for count in (0, 1, 2))
    assert matrix["2"]["interrupt_replay_rejected"] is True
    assert matrix["1"]["commands"][INTEGRATED_DISPATCHES[0]][0]["state"] == "acknowledged"
    assert matrix["2"]["commands"][INTEGRATED_DISPATCHES[0]][0]["state"] == "acknowledged"
    assert matrix["2"]["commands"][INTEGRATED_DISPATCHES[1]][0]["state"] == "rejected"
    assert all(matrix[str(count)]["tui_snapshot_connected"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["tui_headless"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["tui_worker_identity_match"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["tui_control_identity_match"] for count in (0, 1, 2))
    assert all(matrix[str(count)]["steer_observation"].get("steer_marker_applied") == (count > 0) for count in (1, 2))
    assert all(matrix[str(count)]["steer_observation"].get("skill_input_consumed") is True for count in (1, 2))
    assert observed["sdk"]["typed_input_consumed"] is True
    assert observed["sdk"]["structured_output_unchanged"] is True


class _RunningWorkflowHarnessProcess:
    def poll(self) -> None:
        return None


def test_workflow_control_pilot_waits_for_temporary_harness_readiness(tmp_path: Path) -> None:
    harness = WorkflowHarness(tmp_path, worker_command=("provider-must-not-run",))
    thread = threading.Thread(
        target=harness.run_foreground,
        kwargs={"timeout": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        workflow_control_pilot_module._harness_ready(  # pyright: ignore[reportPrivateUsage]
            tmp_path,
            _RunningWorkflowHarnessProcess(),  # type: ignore[arg-type]
            timeout_seconds=2.0,
        )
    finally:
        harness._stop = True  # pyright: ignore[reportPrivateUsage]
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_workflow_control_pilot_waits_for_exact_worker_exit_before_terminal_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = subprocess.Popen((sys.executable, "-c", "import time; time.sleep(0.15)"))
    birth_identity = process_birth_identity(worker.pid)
    reaper = threading.Thread(target=worker.wait, daemon=True)
    reaper.start()

    class FakeLedger:
        def __init__(self, _path: Path) -> None:
            pass

        def queue_dispatch(self, _dispatch_id: str) -> dict[str, object]:
            return {
                "state": "running" if worker.poll() is None else "completed",
                "raw_result_json": "{}",
                "raw_result_sha256": "0" * 64,
            }

        def worker_liveness(self, _dispatch_id: str) -> dict[str, object]:
            return {
                "generation": 1,
                "attempt": 1,
                "pid": worker.pid,
                "process_birth_identity": birth_identity,
                "exited_at": None if worker.poll() is None else "2000-01-01T00:00:00Z",
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_control_pilot_module, "Ledger", FakeLedger)
    queue, live, attempts = workflow_control_pilot_module._wait_for_terminal_result(  # pyright: ignore[reportPrivateUsage]
        tmp_path,
        "pilot/worker/executor/1",
        _RunningWorkflowHarnessProcess(),  # type: ignore[arg-type]
        timeout_seconds=2.0,
    )
    reaper.join(timeout=1.0)
    assert queue["state"] == "completed"
    assert live is not None and live["exited_at"] is not None
    assert attempts == 1


def test_workflow_control_pilot_terminal_wait_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NeverStartedLedger:
        def __init__(self, _path: Path) -> None:
            pass

        def queue_dispatch(self, _dispatch_id: str) -> dict[str, object]:
            return {"state": "queued"}

        def worker_liveness(self, _dispatch_id: str) -> None:
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(workflow_control_pilot_module, "Ledger", NeverStartedLedger)
    with pytest.raises(TimeoutError, match="did not start"):
        workflow_control_pilot_module._wait_for_terminal_result(  # pyright: ignore[reportPrivateUsage]
            tmp_path,
            "pilot/worker/executor/1",
            _RunningWorkflowHarnessProcess(),  # type: ignore[arg-type]
            timeout_seconds=0.02,
        )


def test_workflow_control_pilot_cleanup_stops_temporary_harness_process(tmp_path: Path) -> None:
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(60)"),
        start_new_session=True,
    )
    assert workflow_control_pilot_module._shutdown_harness(  # pyright: ignore[reportPrivateUsage]
        tmp_path,
        process,
        grace_seconds=0.05,
    )
    assert process.poll() is not None
