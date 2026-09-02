"""Agent- and human-facing CLI for the SDK-first workflow controller."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from .app_native import AppNativeDispatchRecord, HostIdentity, HostingMode, HostReceipt
from .backends.codex_sdk import CodexSdkAdapter, CodexSdkConfig, NativeRuntimeConfig
from .config import WorkflowConfigError
from .contracts import (
    ModelFacingControllerActionBundle,
    ModelFacingProgramControllerActionBundle,
    ModelFacingResult,
    model_facing_capsule_schema,
    model_facing_result_schema,
)
from .control_client import ControlClientError, ControllerDecisionClient, LiveWorkerControlClient
from .controller import (
    Controller,
    ControllerError,
    ResumeRequired,
    capsule_json,
    execution_json,
    load_capsule,
    queue_json,
)
from .controller_recovery import ControllerGenerationRecovery, ControllerGenerationRunner
from .controller_recovery_sentinel import (
    run_controller_recovery_sentinel,
    write_controller_recovery_evidence,
)
from .domain import (
    CodexFlowError,
    ControlAcknowledgement,
    ControlCommand,
    ControllerCheckpoint,
    ControllerClaimantKind,
    ConversationHistoryPage,
    ConversationSubjectKind,
    DiagnosticEvent,
    DispatchId,
    ExecutionRecord,
    ExecutionStatus,
    LiveWorkerActivity,
    LiveWorkerStatus,
    NativePermissionAuthority,
    NativePermissionMode,
    ProfileFailure,
    ProgramControllerDecisionStatus,
    ProgramStatus,
    ReasoningEffort,
    RecoveryAction,
    RetryBudgetChange,
    RetryPolicyFacts,
    Sandbox,
    ThreadIdentity,
    strict_json_loads,
)
from .ipc import IpcError, send_request
from .ledger import LedgerError, RecordNotFound, ledger_schema_compatibility
from .live_control_sentinel import run_live_control_sentinel, write_live_control_evidence
from .native_profile import NativeProfileError, NativeProfileProjection
from .plan_capsule import PlanCapsuleError, compile_canonical_plan, compile_program_graph
from .production_pilots import (
    PilotError,
    build_visible_worker_capsule,
    run_production_pilots,
    write_production_evidence,
)
from .program_controller import ProgramControllerGenerationRecovery, ProgramControllerGenerationRunner
from .projection import load_control_capsule, project_model_facing_capsule
from .review_pilots import run_multi_authority_review_pilot, run_review_pilot
from .sdk_compatibility_sentinel import (
    run_sdk_compatibility_sentinel,
    write_sdk_compatibility_evidence,
)
from .service import (
    CredentialUnavailable,
    ServiceError,
    ServiceStartFailed,
    generate_unit,
    install_unit,
    refresh_with_credential,
    start_with_credential,
    systemctl_user,
    unit_path,
)
from .supervisor import Supervisor, SupervisorError
from .worker import WORKER_EXIT_PROFILE, WorkerError, run_nested_delegation_sentinel, submit_result
from .workflow_control_pilot import run_workflow_control_pilot, write_workflow_control_evidence

app = typer.Typer(no_args_is_help=True, help="SDK-first Codex workflow tooling.")
supervisor_app = typer.Typer(no_args_is_help=True, help="Detached repository supervisor lifecycle.")
live_app = typer.Typer(no_args_is_help=True, help="Bounded live-worker observation and control.")
diagnostics_app = typer.Typer(no_args_is_help=True, help="Read-only compatibility, sentinel, and pilot diagnostics.")
controller_decision_app = typer.Typer(no_args_is_help=True, help="Typed controller decision claims and recovery.")
program_app = typer.Typer(no_args_is_help=True, help="Register and operate complete event-driven milestone programs.")
app.add_typer(supervisor_app, name="supervisor")
app.add_typer(live_app, name="live")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(controller_decision_app, name="controller-decision", hidden=True)
app.add_typer(program_app, name="program")
_DEFAULT_STATE_ROOT = Path(".")


@app.callback()
def main() -> None:
    """SDK-first Codex workflow tooling."""


def _resume_argv(thread_id: str) -> tuple[str, str, str]:
    """Return one validated argv tuple; never a shell command."""

    identity = ThreadIdentity(thread_id)
    return ("codex", "resume", identity.id)


@app.command("tui")
def terminal_ui(
    state_root: Annotated[Path, typer.Option(help="Checkout that owns the local supervisor.")] = _DEFAULT_STATE_ROOT,
    dark: Annotated[bool, typer.Option(help="Start with the deterministic dark theme.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Use the monochrome fallback.")] = False,
    resume_mode: Annotated[
        str,
        typer.Option(help="Transcript handoff: print exact argv or open it without a shell."),
    ] = "print",
) -> None:
    """Observe and control detached work through the typed local APIs."""

    if resume_mode not in {"print", "open"}:
        typer.echo("error: --resume-mode must be print or open", err=True)
        raise typer.Exit(code=2)
    from .tui import CodexFlowTerminalApp
    from .tui_client import TerminalUiClient

    def resume_handler(thread_id: str) -> str:
        argv = _resume_argv(thread_id)
        if resume_mode == "print":
            return "Exact resume argv · " + " ".join(argv)
        subprocess.Popen(argv, close_fds=True, shell=False)
        return f"Opened SDK transcript · {argv[2]}"

    terminal = CodexFlowTerminalApp(
        TerminalUiClient.for_state_root(state_root.resolve()),
        resume_handler=resume_handler,
        no_color=no_color,
    )
    terminal.theme = "textual-dark" if dark else "textual-light"
    terminal.run()


def _controller(state_root: Path) -> Controller:
    return Controller(state_root.resolve())


def _emit(record: ExecutionRecord, *, as_json: bool) -> None:
    payload = execution_json(record)
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(
            f"{payload['run_id']}/{payload['milestone_id']} "
            f"status={payload['status']} checkpoint={payload['checkpoint']}"
        )


def _detached_status_payload(controller: Controller, record: ExecutionRecord) -> dict[str, object]:
    """Overlay the detached queue authority on the legacy execution projection."""

    payload: dict[str, object] = dict(execution_json(record))
    dispatch_id = DispatchId.from_parts(record.run_id, record.milestone_id, "executor", 1)
    try:
        queue = controller.ledger.queue_dispatch(dispatch_id)
    except RecordNotFound:
        return payload
    state = queue.get("state")
    thread_id = queue.get("thread_id")
    if isinstance(thread_id, str):
        payload["thread_id"] = thread_id
        if state not in {"completed", "failed", "cancelled"}:
            payload["status"] = ExecutionStatus.THREAD_STARTED.value
            payload["checkpoint"] = ControllerCheckpoint.THREAD_IDENTITY_DURABLE.value
    if state in {"completed", "failed"}:
        raw_result = queue.get("raw_result_json")
        if not isinstance(raw_result, str):
            raise ValueError("terminal detached status is missing its typed result")
        result = ModelFacingResult.from_agent_message(raw_result)
        payload["status"] = state
        payload["checkpoint"] = ControllerCheckpoint.RESULT_DURABLE.value
        payload["result"] = result.to_json()
    elif state == "cancelled":
        payload["status"] = ExecutionStatus.CANCELLED.value
    updated_at = queue.get("updated_at")
    if isinstance(updated_at, str):
        payload["updated_at"] = updated_at
    return payload


def _emit_execution_payload(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(
            f"{payload['run_id']}/{payload['milestone_id']} "
            f"status={payload['status']} checkpoint={payload['checkpoint']}"
        )


def _emit_app_native(record: AppNativeDispatchRecord, *, as_json: bool) -> None:
    payload = record.to_json()
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(f"{record.action.dispatch_id} state={record.state.value} recovery={record.recovery}")


def _program_status_payload(status: object) -> dict[str, object]:
    """Project one durable program status without exposing SQLite rows."""

    if not isinstance(status, ProgramStatus):
        raise ControlClientError("program status is not typed")
    return {
        "program_id": str(status.program_id),
        "state": status.state.value,
        "revision": status.revision,
        "plan_digest": status.plan_digest,
        "trunk_head": status.trunk_head,
        "nodes": [
            {
                "milestone_id": str(node.milestone_id),
                "state": node.state,
                "dependencies": [str(item) for item in node.dependencies],
                "candidate_sha": node.candidate_sha,
                "review_ids": list(node.review_ids),
                "finding_ids": list(node.finding_ids),
                "integrated": node.integrated,
            }
            for node in status.nodes
        ],
        "ready_milestones": [str(item) for item in status.ready_milestones],
    }


def _program_decision_payload(status: object) -> dict[str, object]:
    """Project one event-driven program decision for operators."""

    if not isinstance(status, ProgramControllerDecisionStatus):
        raise ControlClientError("program decision status is not typed")
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
        "payload": status.payload,
    }


def _parse_program_dependencies(values: list[str] | None) -> dict[str, tuple[str, ...]]:
    """Parse repeated ``milestone=dependency[,dependency]`` graph options."""

    parsed: dict[str, tuple[str, ...]] = {}
    for value in values or []:
        milestone, separator, dependencies = value.partition("=")
        if not separator or not milestone or not dependencies:
            raise ValueError("--dependency must be milestone=dependency[,dependency]")
        items = tuple(item for item in dependencies.split(",") if item)
        if not items or milestone in parsed:
            raise ValueError("--dependency entries must name one unique milestone and dependency list")
        parsed[milestone] = items
    return parsed


def _emit_program_payload(payload: object, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


@program_app.command("register")
def program_register(
    program_id: Annotated[str, typer.Option(help="Stable program identity.")],
    plan_path: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Canonical plan containing the typed milestone capsules.")
    ],
    milestone_ids: Annotated[
        list[str] | None,
        typer.Option("--milestone-id", help="Milestone id; repeat once for each program node."),
    ] = None,
    dependencies: Annotated[
        list[str] | None,
        typer.Option(
            "--dependency",
            help="Dependency edge as milestone=dependency[,dependency]; repeat for each dependent milestone.",
        ),
    ] = None,
    trunk_head: Annotated[
        str | None, typer.Option(help="Expected trunk commit; default is the current Git HEAD.")
    ] = None,
    integration_strategy: Annotated[
        str, typer.Option(help="Git integration strategy for promoted candidates.")
    ] = "merge",
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Compile and durably register one closed milestone DAG."""

    controller: Controller | None = None
    try:
        compiled = compile_program_graph(
            plan_path,
            program_id,
            tuple(milestone_ids) if milestone_ids is not None else None,
            _parse_program_dependencies(dependencies),
        )
        graph = compiled.to_program_graph(state_root.resolve(), trunk_head=trunk_head)
        graph = replace(graph, integration_strategy=integration_strategy)
        controller = _controller(state_root)
        _emit_program_payload(_program_status_payload(controller.register_program(graph)), as_json=as_json)
    except (ControllerError, ControlClientError, PlanCapsuleError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()


@program_app.command("start")
def program_start(
    program_id: Annotated[str, typer.Option(help="Stable program identity.")],
    event_key: Annotated[str, typer.Option(help="Idempotent start event key.")] = "start",
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Emit one coalesced start event to the detached supervisor."""

    controller: Controller | None = None
    try:
        controller = _controller(state_root)
        decision = controller.start_program(program_id, event_key=event_key)
        _emit_program_payload(_program_decision_payload(decision), as_json=as_json)
    except (ControllerError, ControlClientError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()


@program_app.command("status")
def program_status(
    program_id: Annotated[str, typer.Option(help="Stable program identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Read one durable program projection without starting a worker."""

    controller: Controller | None = None
    try:
        controller = _controller(state_root)
        _emit_program_payload(_program_status_payload(controller.program_status(program_id)), as_json=as_json)
    except (ControllerError, ControlClientError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()


@program_app.command("decisions")
def program_decisions(
    program_id: Annotated[str | None, typer.Option(help="Optional stable program identity filter.")] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """List durable program events awaiting or completing controller work."""

    try:
        decisions = _controller_decision_client(state_root).pending()
        if program_id is not None:
            decisions = tuple(item for item in decisions if str(item.program_id) == program_id)
        _emit_program_payload([_program_decision_payload(item) for item in decisions], as_json=as_json)
    except (ControlClientError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@program_app.command("decide")
def program_decide(
    decision_id: Annotated[str, typer.Option(help="Exact durable decision/<wake> identity.")],
    bundle: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Closed program action bundle JSON.")],
    revision: Annotated[int, typer.Option(help="Expected decision revision for the human CAS claim.")],
    claimant_id: Annotated[str, typer.Option(help="Stable human claimant identity.")] = "program-human",
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Claim, submit, and acknowledge one bounded human program decision."""

    try:
        raw = strict_json_loads(bundle.read_bytes(), max_bytes=32 * 1024)
        if not isinstance(raw, dict):
            raise ValueError("program bundle must be an object")
        typed_bundle = ModelFacingProgramControllerActionBundle.from_json(raw)
        client = _controller_decision_client(state_root)
        claim = client.program_claim(
            decision_id,
            claimant_id=claimant_id,
            expected_revision=revision,
            generation=int(typed_bundle.generation),
            claimant_kind=ControllerClaimantKind.HUMAN,
        )
        receipt = client.submit_program_actions(typed_bundle, claimant_id=claim.claimant_id, token=str(claim.token))
        acknowledged = client.acknowledge_program(
            decision_id,
            action_id=receipt.action_id,
            bundle_sha256=typed_bundle.sha256,
            committed_revision=receipt.expected_revision + 1,
            claimant_id=claim.claimant_id,
            token=str(claim.token),
        )
        _emit_program_payload(
            {
                "action_id": acknowledged.action_id,
                "decision_id": str(acknowledged.decision_id),
                "generation": int(acknowledged.generation),
                "expected_revision": acknowledged.expected_revision,
                "state": acknowledged.state,
                "bundle_sha256": acknowledged.bundle_sha256,
                "effect_receipt": acknowledged.effect_receipt,
                "committed_at": acknowledged.committed_at,
                "acknowledged_at": acknowledged.acknowledged_at,
            },
            as_json=as_json,
        )
    except (ControlClientError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@diagnostics_app.command("schema-compatibility")
def schema_compatibility(
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
) -> None:
    """Inspect candidate/ledger compatibility without opening the live controller."""

    try:
        payload = ledger_schema_compatibility(state_root.resolve() / ".codex-flow" / "workflow.db")
    except (LedgerError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if not payload["compatible"]:
        raise typer.Exit(code=2)


@app.command("plan", hidden=True)
def plan_execution(
    capsule: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Versioned execution capsule JSON.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Validate and durably record one execution capsule without starting Codex."""

    controller = _controller(state_root)
    try:
        parsed, _digest = load_capsule(capsule)
        _emit(controller.plan(parsed), as_json=as_json)
    except (ControllerError, TypeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()


def _execution_action(
    action: str,
    run_id: str,
    milestone_id: str,
    state_root: Path,
    as_json: bool,
) -> None:
    resolved_root = state_root.resolve()
    if not (resolved_root / ".codex-flow" / "workflow.db").is_file():
        typer.echo("error: controller ledger does not exist", err=True)
        raise typer.Exit(code=2)
    controller = _controller(resolved_root)
    try:
        method = {
            "start": controller.start,
            "resume": controller.resume,
            "status": controller.status,
            "cancel": controller.cancel,
        }[action]
        record = method(run_id, milestone_id)
        if action == "status":
            _emit_execution_payload(_detached_status_payload(controller, record), as_json=as_json)
        else:
            _emit(record, as_json=as_json)
    except (ControllerError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()


@app.command("start", hidden=True)
def start_execution(
    run_id: Annotated[str, typer.Option(help="Durable run identity.")],
    milestone_id: Annotated[str, typer.Option(help="Durable milestone identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start exactly one planned SDK execution."""

    _execution_action("start", run_id, milestone_id, state_root, as_json)


@app.command("resume", hidden=True)
def resume_execution(
    run_id: Annotated[str, typer.Option(help="Durable run identity.")],
    milestone_id: Annotated[str, typer.Option(help="Durable milestone identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resume one durable SDK thread in a fresh process."""

    _execution_action("resume", run_id, milestone_id, state_root, as_json)


@app.command("status")
def execution_status(
    run_id: Annotated[str, typer.Option(help="Durable run identity.")],
    milestone_id: Annotated[str, typer.Option(help="Durable milestone identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read stable execution status without side effects."""

    _execution_action("status", run_id, milestone_id, state_root, as_json)


@app.command("cancel")
def cancel_execution(
    run_id: Annotated[str, typer.Option(help="Durable run identity.")],
    milestone_id: Annotated[str, typer.Option(help="Durable milestone identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Cancel idempotently without deleting a workspace or SDK thread."""

    _execution_action("cancel", run_id, milestone_id, state_root, as_json)


@app.command("control")
def control_execution(
    capsule: Annotated[
        Path | None,
        typer.Option("--capsule", exists=True, dir_okay=False, help="Explicit low-level capsule JSON for tests."),
    ] = None,
    plan_path: Annotated[
        Path | None,
        typer.Option(
            "--plan-path", exists=True, dir_okay=False, help="Canonical plan containing the active typed capsule."
        ),
    ] = None,
    plan_milestone_id: Annotated[
        str | None,
        typer.Option("--milestone-id", help="Exact active milestone id in --plan-path."),
    ] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Explicitly resume an existing durable SDK identity."),
    ] = False,
    hosting: Annotated[
        HostingMode,
        typer.Option(help="Execution host: existing SDK-headless control or one host-mediated App-native action."),
    ] = HostingMode.SDK_HEADLESS,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
    checkpoint_seconds: Annotated[
        float,
        typer.Option("--checkpoint-seconds", help="One-shot source-controller checkpoint delay."),
    ] = 1800.0,
) -> None:
    """Plan once, start once, and report the controller's durable status.

    This is the packaged agent entrypoint.  It does not create native peer
    tasks or retry an uncertain external call.  Use ``--resume`` only after a
    prior status explicitly reports a durable SDK identity.
    """

    controller: Controller | None = None
    try:
        # Decode and project before constructing Controller: malformed or
        # authority-invalid model input must not create .codex-flow state.
        compiled = None
        if plan_path is not None or plan_milestone_id is not None:
            if plan_path is None or plan_milestone_id is None or capsule is not None:
                raise ControllerError("control requires either --capsule or --plan-path with --milestone-id")
            try:
                compiled = compile_canonical_plan(plan_path, plan_milestone_id)
                parsed = project_model_facing_capsule(compiled.capsule, state_root=state_root)
            except PlanCapsuleError as exc:
                raise ControllerError(str(exc)) from exc
        elif capsule is not None:
            parsed, _digest = load_control_capsule(capsule, state_root=state_root)
        else:
            raise ControllerError("control requires --plan-path with --milestone-id")
        controller = _controller(state_root)
        try:
            planned = controller.plan(parsed)
        except (ControllerError, LedgerError) as planning_error:
            # Only a terminal existing plan may be reconciled read-only.  Do
            # not hide a conflicting capsule, scope violation, or integrity
            # failure behind a status lookup.
            existing = controller.status(parsed.run_id, parsed.milestone_id)
            if existing.status not in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
                raise planning_error
            expected_digest = hashlib.sha256(
                (
                    json.dumps(
                        capsule_json(parsed), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    + "\n"
                ).encode("utf-8")
            ).hexdigest()
            if existing.capsule_sha256 != expected_digest:
                raise planning_error
            planned = existing
        if hosting is HostingMode.APP_NATIVE:
            if resume:
                raise ControllerError("--resume is only valid for SDK-headless control")
            _emit_app_native(
                controller.prepare_app_native(planned.run_id, planned.milestone_id),
                as_json=as_json,
            )
            return
        # The production entrypoint is a short-lived queue producer. Hermetic
        # tests with an injected adapter retain the historical synchronous
        # control path so the adapter contract remains covered.
        if controller._production_adapter:
            socket_path = controller.state_dir / "runtime" / "supervisor.sock"
            if not socket_path.exists() or socket_path.is_symlink():
                raise ControllerError(
                    "detached supervisor is unavailable; start the installed supervisor service before enqueue"
                )
            queued = controller.enqueue(
                parsed,
                backend="sdk_headless",
                plan_path=(compiled.plan_path if compiled is not None else "internal"),
                plan_revision_sha256=(compiled.plan_revision_sha256 if compiled is not None else None),
                checkpoint_seconds=checkpoint_seconds,
            )
            try:
                send_request(
                    socket_path,
                    {"version": 1, "operation": "wake"},
                    timeout=1.0,
                )
            except (IpcError, OSError) as exc:
                raise ControllerError(
                    "detached supervisor is unavailable; start the installed supervisor service before enqueue"
                ) from exc
            if as_json:
                typer.echo(json.dumps(queue_json(queued), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            else:
                typer.echo(f"{queued['dispatch_id']} state={queued['state']}")
            return
        if resume:
            record = controller.resume(planned.run_id, planned.milestone_id)
        else:
            try:
                record = controller.start(planned.run_id, planned.milestone_id)
            except ResumeRequired:
                record = controller.status(planned.run_id, planned.milestone_id)
        _emit(record, as_json=as_json)
    except (ControllerError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()


@app.command("app-bind", hidden=True)
def bind_app_native(
    dispatch_id: Annotated[str, typer.Option(help="Exact prepared logical dispatch identity.")],
    claim_token: Annotated[str, typer.Option(help="Capability returned only by App-native prepare.")],
    receipt: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Closed one-time host receipt JSON.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Bind exactly one returned host/thread identity to its prepared action."""

    controller = _controller(state_root)
    try:
        decoded = strict_json_loads(receipt.read_bytes())
        if not isinstance(decoded, dict):
            raise ValueError("host receipt root must be an object")
        typed_receipt = HostReceipt.from_json(decoded)
        record = controller.bind_app_native(
            dispatch_id,
            claim_token=claim_token,
            receipt=typed_receipt,
        )
        _emit_app_native(record, as_json=as_json)
    except (ControllerError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()


@app.command("app-result", hidden=True)
def ingest_app_native_result(
    dispatch_id: Annotated[str, typer.Option(help="Exact bound logical dispatch identity.")],
    claim_token: Annotated[str, typer.Option(help="Capability returned only by App-native prepare.")],
    thread_id: Annotated[str, typer.Option(help="Exact bound native thread identity.")],
    host_id: Annotated[str, typer.Option(help="Exact bound hosting Codex app identity.")],
    result: Annotated[
        Path | None, typer.Option("--result", exists=True, dir_okay=False, help="Legacy typed result JSON.")
    ] = None,
    agent_message: Annotated[
        Path | None,
        typer.Option("--agent-message", exists=True, dir_okay=False, help="Complete raw terminal agentMessage JSON."),
    ] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate and durably ingest one terminal result from the bound worker."""

    controller: Controller | None = None
    try:
        if (result is None) == (agent_message is None):
            raise ValueError("provide exactly one of --agent-message or --result")
        if agent_message is not None:
            typed_result = ModelFacingResult.from_agent_message(agent_message.read_bytes())
        else:
            decoded = strict_json_loads(result.read_bytes())
            if not isinstance(decoded, dict):
                raise ValueError("App-native result root must be an object")
            typed_result = ModelFacingResult.from_json(decoded)
        controller = _controller(state_root)
        record = controller.complete_app_native(
            dispatch_id,
            claim_token=claim_token,
            identity=HostIdentity(host_id, ThreadIdentity(thread_id)),
            result=typed_result,
        )
        _emit_app_native(record, as_json=as_json)
    except (ControllerError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()


@app.command("worker-submit", hidden=True)
def worker_submit(
    capability_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    result_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    socket_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Submit one complete raw ModelFacingResult from an external worker."""

    try:
        response = submit_result(capability_file=capability_file, result_file=result_file, socket_path=socket_path)
    except (WorkerError, IpcError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@diagnostics_app.command("worker-sentinel")
def worker_sentinel() -> None:
    """Run the leaf-worker nested-delegation adversarial sentinel."""

    typer.echo(
        json.dumps(
            run_nested_delegation_sentinel(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@live_app.command("conversation")
def live_conversation(
    subject_kind: Annotated[
        ConversationSubjectKind,
        typer.Option(help="Native persisted subject kind: worker or controller."),
    ],
    subject_id: Annotated[str, typer.Option(help="Exact worker dispatch or controller decision identity.")],
    thread_id: Annotated[str, typer.Option(help="Exact native SDK thread identity.")],
    generation: Annotated[int | None, typer.Option(help="Bound generation, when required by the subject.")] = None,
    attempt: Annotated[int | None, typer.Option(help="Bound worker process attempt, when required.")] = None,
    revision: Annotated[int | None, typer.Option(help="Bound controller decision revision, when required.")] = None,
    page_token: Annotated[str | None, typer.Option(help="Opaque token returned for the next older page.")] = None,
    page_fragments: Annotated[int, typer.Option(help="Maximum fragments in one bounded page (1-32).")] = 32,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns the supervisor.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read one bounded native SDK conversation page through the supervisor."""

    try:
        page = LiveWorkerControlClient.for_state_root(state_root).conversation_history(
            subject_kind=subject_kind.value,
            subject_id=subject_id,
            thread_id=thread_id,
            generation=generation,
            attempt=attempt,
            revision=revision,
            page_token=page_token,
            page_fragments=page_fragments,
        )
        _emit_control(page, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _control_payload(value: object) -> object:
    if isinstance(
        value,
        ControlAcknowledgement
        | ControlCommand
        | ConversationHistoryPage
        | DiagnosticEvent
        | LiveWorkerActivity
        | LiveWorkerStatus
        | RecoveryAction
        | RetryPolicyFacts,
    ):
        return value.to_json()  # type: ignore[no-any-return]
    if isinstance(value, tuple):
        return [_control_payload(item) for item in value]
    return value


def _emit_control(payload: object, *, as_json: bool) -> None:
    payload = _control_payload(payload)
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


@live_app.command("status")
def live_status(
    dispatch_id: Annotated[str | None, typer.Option(help="Optional exact dispatch identity.")] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read queue, retry-policy and bounded recent activity facts."""

    try:
        _emit_control(LiveWorkerControlClient.for_state_root(state_root).status(dispatch_id), as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("activity")
def live_activity(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    limit: Annotated[int, typer.Option(help="Maximum recent events (1-128).")] = 128,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read bounded redacted activity without mutating worker state."""

    try:
        _emit_control(
            LiveWorkerControlClient.for_state_root(state_root).recent_activity(dispatch_id, limit=limit),
            as_json=as_json,
        )
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("steer")
def live_steer(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    generation: Annotated[int, typer.Option(help="Exact dispatch generation.")],
    attempt: Annotated[int, typer.Option(help="Exact process attempt.")],
    thread_id: Annotated[str, typer.Option(help="Exact SDK thread identity.")],
    turn_id: Annotated[str, typer.Option(help="Exact in-flight SDK turn identity.")],
    text: Annotated[str, typer.Option(help="Bounded steer text (at most 8 KiB).")],
    command_id: Annotated[str | None, typer.Option(help="Optional idempotency identity.")] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Queue one exact-turn steer command."""

    try:
        result = LiveWorkerControlClient.for_state_root(state_root).steer(
            dispatch_id,
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
            text=text,
            command_id=command_id,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("interrupt")
def live_interrupt(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    generation: Annotated[int, typer.Option(help="Exact dispatch generation.")],
    attempt: Annotated[int, typer.Option(help="Exact process attempt.")],
    thread_id: Annotated[str, typer.Option(help="Exact SDK thread identity.")],
    turn_id: Annotated[str, typer.Option(help="Exact in-flight SDK turn identity.")],
    command_id: Annotated[str | None, typer.Option(help="Optional idempotency identity.")] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Queue one exact-turn interrupt command."""

    try:
        result = LiveWorkerControlClient.for_state_root(state_root).interrupt(
            dispatch_id,
            generation=generation,
            attempt=attempt,
            thread_id=thread_id,
            turn_id=turn_id,
            command_id=command_id,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("retry")
def live_retry(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    action_id: Annotated[str, typer.Option(help="Idempotent recovery action identity.")],
    expected_revision: Annotated[int, typer.Option(help="Expected retry-policy revision.")],
    reason: Annotated[str, typer.Option(help="Explicit bounded human/controller reason.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Apply one compare-and-swap retry decision to an attention state."""

    try:
        result = LiveWorkerControlClient.for_state_root(state_root).retry(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            reason=reason,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("cancel")
def live_cancel(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    action_id: Annotated[str, typer.Option(help="Idempotent recovery action identity.")],
    expected_revision: Annotated[int, typer.Option(help="Expected retry-policy revision.")],
    reason: Annotated[str, typer.Option(help="Explicit bounded human/controller reason.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Apply one compare-and-swap cancellation action."""

    try:
        result = LiveWorkerControlClient.for_state_root(state_root).cancel(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            reason=reason,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("compatibility-rebind")
def live_compatibility_rebind(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    action_id: Annotated[str, typer.Option(help="One-shot recovery action identity.")],
    expected_revision: Annotated[int, typer.Option(help="Expected retry-policy revision.")],
    reason: Annotated[str, typer.Option(help="Explicit bounded human authorization reason.")],
    expected_compatibility_sha256: Annotated[str, typer.Option(help="Exact queued worker-compatibility digest.")],
    proposed_compatibility_sha256: Annotated[
        str, typer.Option(help="Exact verified installed worker-compatibility digest.")
    ],
    expected_profile_sha256: Annotated[str, typer.Option(help="Exact queued native-profile digest.")],
    proposed_profile_sha256: Annotated[str, typer.Option(help="Exact verified installed native-profile digest.")],
    expected_generation: Annotated[int, typer.Option(help="Exact queued worker generation.")],
    expected_attempt: Annotated[int, typer.Option(help="Exact queued worker attempt.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Authorize one exact installed-runtime compatibility rebind without retrying."""

    try:
        result = LiveWorkerControlClient.for_state_root(state_root).rebind_compatibility(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            reason=reason,
            expected_compatibility_sha256=expected_compatibility_sha256,
            proposed_compatibility_sha256=proposed_compatibility_sha256,
            expected_profile_sha256=expected_profile_sha256,
            proposed_profile_sha256=proposed_profile_sha256,
            expected_generation=expected_generation,
            expected_attempt=expected_attempt,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@live_app.command("budget-change")
def live_budget_change(
    dispatch_id: Annotated[str, typer.Option(help="Exact dispatch identity.")],
    action_id: Annotated[str, typer.Option(help="Idempotent recovery action identity.")],
    expected_revision: Annotated[int, typer.Option(help="Expected retry-policy revision.")],
    reason: Annotated[str, typer.Option(help="Explicit bounded human/controller reason.")],
    pre_identity_budget: Annotated[int, typer.Option(help="Absolute pre-identity retry ceiling (0-5).")],
    invalid_chain_budget: Annotated[int, typer.Option(help="Absolute invalid-chain retry ceiling (0-1).")],
    schema_envelope_budget: Annotated[int, typer.Option(help="Absolute schema-envelope retry ceiling (0-2).")],
    post_identity_loss_budget: Annotated[int, typer.Option(help="Absolute post-identity retry ceiling (0-1).")],
    provider_transient_budget: Annotated[
        int | None, typer.Option(help="Exact provider-transient ceiling; 4 is the one authorized grant.")
    ] = None,
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Apply one compare-and-swap bounded retry-budget change."""

    try:
        budget = RetryBudgetChange(
            pre_identity_budget=pre_identity_budget,
            invalid_chain_budget=invalid_chain_budget,
            schema_envelope_budget=schema_envelope_budget,
            post_identity_loss_budget=post_identity_loss_budget,
            provider_transient_budget=provider_transient_budget,
        )
        result = LiveWorkerControlClient.for_state_root(state_root).change_budget(
            dispatch_id,
            action_id=action_id,
            expected_revision=expected_revision,
            reason=reason,
            requested_budget=budget,
        )
        _emit_control(result, as_json=as_json)
    except (ControlClientError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@supervisor_app.command("run")
def supervisor_run(
    state_root: Annotated[
        Path, typer.Option(help="Repository checkout owning .codex-flow state.")
    ] = _DEFAULT_STATE_ROOT,
    foreground: Annotated[bool, typer.Option("--foreground", help="Run the supervisor in this process.")] = False,
) -> None:
    if not foreground:
        typer.echo("error: supervisor run requires --foreground", err=True)
        raise typer.Exit(code=2)
    supervisor = Supervisor(state_root.resolve())
    try:
        supervisor.run_foreground()
    except (SupervisorError, LedgerError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        supervisor.close()


@supervisor_app.command("rebind-legacy")
def supervisor_rebind_legacy(
    dispatch_id: Annotated[str, typer.Option(help="Exact migrated active v10 dispatch identity.")],
    source_thread_id: Annotated[str, typer.Option(help="Exact source controller task identity.")],
    state_root: Annotated[
        Path, typer.Option(help="Repository checkout owning .codex-flow state.")
    ] = _DEFAULT_STATE_ROOT,
) -> None:
    """Atomically restore verified native authority for one migrated v10 queue."""

    supervisor = Supervisor(state_root.resolve())
    try:
        binding = supervisor.rebind_legacy_active_queue(
            dispatch_id,
            source_thread_id=source_thread_id,
        )
        typer.echo(
            json.dumps(
                {
                    "dispatch_id": binding["dispatch_id"],
                    "source_thread_id": binding["source_thread_id"],
                    "permission_mode": binding["permission_mode"],
                    "native_profile_sha256": binding["native_profile_sha256"],
                    "native_compatibility_sha256": binding["native_compatibility_sha256"],
                    "checkpoint_armed": bool(binding["checkpoint_armed"]),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (SupervisorError, LedgerError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        supervisor.close()


def _service_profile() -> NativeProfileProjection:
    native_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return NativeProfileProjection.load(native_home)


def _service_unit(
    state_root: Path,
    *,
    profile: NativeProfileProjection | None = None,
    allow_missing_profile: bool = False,
):
    import sys

    selected = profile
    if selected is None:
        try:
            selected = _service_profile()
        except (NativeProfileError, OSError, ValueError):
            # Read-only status/stop/uninstall still address the exact unit
            # name when no credential is available; explicit start/install
            # remain fail-closed through their profile path.
            if not allow_missing_profile:
                raise
            selected = None
    return generate_unit(
        state_root.resolve(),
        state_root=state_root.resolve(),
        executable=Path(sys.argv[0]).resolve(),
        version="0.2.0",
        provider_env_key=selected.provider_env_key if selected is not None else None,
        profile_sha256=selected.profile_sha256 if selected is not None else None,
    )


@supervisor_app.command("install")
def supervisor_install(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    try:
        typer.echo(os.fspath(install_unit(_service_unit(state_root))))
    except (ServiceError, NativeProfileError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@supervisor_app.command("start")
def supervisor_start(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    try:
        profile = _service_profile()
        unit = _service_unit(state_root, profile=profile)
        start_with_credential(
            unit,
            provider_env_key=profile.provider_env_key,
            profile_sha256=profile.profile_sha256,
        )
    except (CredentialUnavailable, ServiceStartFailed, ServiceError, NativeProfileError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@supervisor_app.command("status")
def supervisor_status(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    try:
        unit = _service_unit(state_root, allow_missing_profile=True)
        systemctl_user("status", unit)
        typer.echo(
            json.dumps(
                {"unit": unit.unit_name, "path": os.fspath(unit_path(repository_root=state_root.resolve()))},
                separators=(",", ":"),
            )
        )
    except (ServiceError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@supervisor_app.command("stop")
def supervisor_stop(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    try:
        systemctl_user("stop", _service_unit(state_root, allow_missing_profile=True))
    except (ServiceError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@supervisor_app.command("uninstall")
def supervisor_uninstall(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    path = unit_path(repository_root=state_root.resolve())
    try:
        if path.is_symlink():
            raise ServiceError("service unit path is a symlink")
        if path.exists():
            path.unlink()
    except (ServiceError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command("app-status", hidden=True)
def app_native_status(
    dispatch_id: Annotated[str, typer.Option(help="Prepared App-native logical dispatch identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Expose durable App-native identity, terminal, and recovery facts."""

    controller = _controller(state_root)
    try:
        _emit_app_native(controller.app_native_status(dispatch_id), as_json=as_json)
    except (ControllerError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()


@supervisor_app.command("refresh")
def supervisor_refresh(state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT) -> None:
    """Install and restart one already-installed repository supervisor unit."""

    try:
        profile = _service_profile()
        unit = _service_unit(state_root, profile=profile)
        refresh_with_credential(
            unit,
            provider_env_key=profile.provider_env_key,
            profile_sha256=profile.profile_sha256,
            native_compatibility_sha256=profile.worker_compatibility_sha256,
        )
    except (CredentialUnavailable, ServiceStartFailed, ServiceError, NativeProfileError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command("schema")
def model_schema(
    kind: Annotated[str, typer.Option(help="Schema to print: capsule, result, or all.")] = "all",
) -> None:
    """Print the typed model-facing capsule/result JSON schemas."""

    if kind not in {"capsule", "result", "all"}:
        typer.echo("error: kind must be capsule, result, or all", err=True)
        raise typer.Exit(code=2)
    payload: object
    if kind == "capsule":
        payload = model_facing_capsule_schema()
    elif kind == "result":
        payload = model_facing_result_schema()
    else:
        payload = {"capsule": model_facing_capsule_schema(), "result": model_facing_result_schema()}
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


@diagnostics_app.command("sdk-compatibility-sentinel")
def sdk_compatibility_sentinel(
    real: Annotated[bool, typer.Option(help="Explicitly authorize starting the local SDK runtime.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained JSON evidence path."),
    ] = Path(".codex-flow/h1-sdk-sentinel.json"),
) -> None:
    """Probe the pinned SDK capabilities in a disposable read-only repository."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_sdk_compatibility_sentinel(
        model=model,
        effort=effort,
    )
    write_sdk_compatibility_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("controller-recovery-sentinel")
def controller_recovery_sentinel(
    real: Annotated[bool, typer.Option(help="Explicitly authorize SDK editing in a disposable repository.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained JSON evidence path."),
    ] = Path(".codex-flow/h3-controller-sentinel.json"),
) -> None:
    """Prove post-identity crash recovery through the real SDK controller."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_controller_recovery_sentinel(model=model, effort=effort)
    write_controller_recovery_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("live-control-sentinel")
def live_control_sentinel(
    real: Annotated[bool, typer.Option(help="Explicitly authorize one bounded real SDK steer attempt.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized JSON evidence path."),
    ] = Path("docs/reviews/evidence/live-worker-control.json"),
) -> None:
    """Run one exact-wheel delayed-turn steer sentinel in a read-only repository."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_live_control_sentinel(model=model, effort=effort)
    write_live_control_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("review-pilot")
def review_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized review-pilot evidence path."),
    ] = Path("docs/reviews/evidence/h4-a-objective-pilot.json"),
) -> None:
    """Run one bounded review/review-repair/fresh-review SDK pilot."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_review_pilot(model=model, effort=effort)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("multi-authority-review-pilot")
def multi_authority_review_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized multi-authority review evidence path."),
    ] = Path("docs/reviews/evidence/h4-b-multi-authority-pilot.json"),
) -> None:
    """Run one bounded multi-authority review SDK pilot."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_multi_authority_review_pilot(model=model, effort=effort)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("workflow-control-pilot")
def workflow_control_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized workflow-control evidence path."),
    ] = Path("docs/reviews/evidence/h5-workflow-control-medium.json"),
) -> None:
    """Run one bounded milestone through workflow-control/codex-flow."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_workflow_control_pilot(model=model, effort=effort)
    write_workflow_control_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@diagnostics_app.command("production-pilots")
def production_pilots(
    real: Annotated[
        bool,
        typer.Option(help="Explicitly authorize the fixed medium and large production pilots."),
    ] = False,
    config: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="Repository-owned workflow route configuration."),
    ] = Path("workflow.toml"),
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized production-pilot evidence path."),
    ] = Path("docs/reviews/evidence/h6-production-pilots.json"),
) -> None:
    """Run integrated production pilots and the retirement decision."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    try:
        evidence = run_production_pilots(config_path=config.resolve())
    except (CodexFlowError, PilotError, WorkflowConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    write_production_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")


@app.command("visible-worker-capsule", hidden=True)
def visible_worker_capsule(
    parent_run_id: Annotated[str, typer.Option(help="Completed visible-worker run that owns the workspace lease.")],
    state_root: Annotated[Path, typer.Option(help="Exact existing controller checkout.")] = _DEFAULT_STATE_ROOT,
    output: Annotated[
        Path | None,
        typer.Option(help="Optional exact controller-owned durable capsule path."),
    ] = None,
) -> None:
    """Plan and return the canonical durable capsule for a visible worker."""

    controller: Controller | None = None
    try:
        capsule = build_visible_worker_capsule(
            state_root=state_root,
            parent_run_id=parent_run_id,
        )
        expected = (
            state_root.resolve() / ".codex-flow" / "capsules" / str(capsule.run_id) / f"{capsule.milestone_id}.json"
        )
        if output is not None and output.resolve() != expected:
            raise ValueError(f"--output must equal the controller-owned durable path: {expected}")
        controller = _controller(state_root)
        record = controller.plan(capsule)
    except (PilotError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()
    typer.echo(str(record.capsule_path))


def _controller_decision_client(state_root: Path) -> ControllerDecisionClient:
    return ControllerDecisionClient.for_state_root(state_root)


def _controller_decision_payload(status: object) -> dict[str, object]:
    from .domain import ControllerDecisionStatus

    if not isinstance(status, ControllerDecisionStatus):
        raise ControlClientError("controller decision status is not typed")
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


@controller_decision_app.command("list")
def controller_decision_list(
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
) -> None:
    """List durable controller decisions."""

    try:
        payload = [_controller_decision_payload(item) for item in _controller_decision_client(state_root).pending()]
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    except (ControlClientError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@controller_decision_app.command("status")
def controller_decision_status(
    decision_id: Annotated[str, typer.Option(help="Durable decision/<wake> identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
) -> None:
    """Show one typed controller decision."""

    try:
        status = _controller_decision_client(state_root).status(decision_id)
        typer.echo(json.dumps(_controller_decision_payload(status), sort_keys=True))
    except (ControlClientError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@controller_decision_app.command("claim")
def controller_decision_claim(
    decision_id: Annotated[str, typer.Option()],
    claimant_id: Annotated[str, typer.Option()],
    revision: Annotated[int, typer.Option()],
    human: Annotated[bool, typer.Option(help="Claim as a human operator.")] = False,
    state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT,
) -> None:
    """Claim one decision through the typed CAS boundary."""

    try:
        from .domain import ControllerClaimantKind

        claim = _controller_decision_client(state_root).claim(
            decision_id,
            claimant_id=claimant_id,
            expected_revision=revision,
            claimant_kind=ControllerClaimantKind.HUMAN if human else ControllerClaimantKind.MODEL,
        )
        typer.echo(
            json.dumps(
                {
                    "decision_id": str(claim.decision_id),
                    "generation": int(claim.generation),
                    "claimant_kind": claim.claimant_kind.value,
                    "claimant_id": claim.claimant_id,
                    "revision": claim.revision,
                    "lease_expires_at": claim.lease_expires_at,
                    "token": claim.token,
                },
                sort_keys=True,
            )
        )
    except (ControlClientError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@controller_decision_app.command("submit")
def controller_decision_submit(
    bundle: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    claimant_id: Annotated[str, typer.Option()],
    token: Annotated[str, typer.Option()],
    state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT,
) -> None:
    """Submit one closed action bundle."""

    try:
        raw = strict_json_loads(bundle.read_bytes(), max_bytes=32 * 1024)
        if not isinstance(raw, dict):
            raise ValueError("bundle must be an object")
        receipt = _controller_decision_client(state_root).submit_actions(
            ModelFacingControllerActionBundle.from_json(raw), claimant_id=claimant_id, token=token
        )
        typer.echo(
            json.dumps(
                {
                    "action_id": receipt.action_id,
                    "decision_id": str(receipt.decision_id),
                    "generation": int(receipt.generation),
                    "expected_revision": receipt.expected_revision,
                    "state": receipt.state,
                    "bundle_sha256": receipt.bundle_sha256,
                    "effect_receipt": receipt.effect_receipt,
                    "committed_at": receipt.committed_at,
                    "acknowledged_at": receipt.acknowledged_at,
                },
                sort_keys=True,
            )
        )
    except (ControlClientError, ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@controller_decision_app.command("acknowledge")
def controller_decision_acknowledge(
    decision_id: Annotated[str, typer.Option()],
    action_id: Annotated[str, typer.Option()],
    bundle_sha256: Annotated[str, typer.Option()],
    committed_revision: Annotated[int, typer.Option()],
    claimant_id: Annotated[str, typer.Option()],
    token: Annotated[str, typer.Option()],
    state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT,
) -> None:
    """Acknowledge one committed action receipt."""

    try:
        receipt = _controller_decision_client(state_root).acknowledge(
            decision_id,
            action_id=action_id,
            bundle_sha256=bundle_sha256,
            committed_revision=committed_revision,
            claimant_id=claimant_id,
            token=token,
        )
        typer.echo(
            json.dumps(
                {
                    "action_id": receipt.action_id,
                    "decision_id": str(receipt.decision_id),
                    "generation": int(receipt.generation),
                    "expected_revision": receipt.expected_revision,
                    "state": receipt.state,
                    "bundle_sha256": receipt.bundle_sha256,
                    "effect_receipt": receipt.effect_receipt,
                    "committed_at": receipt.committed_at,
                    "acknowledged_at": receipt.acknowledged_at,
                },
                sort_keys=True,
            )
        )
    except (ControlClientError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@controller_decision_app.command("recover")
def controller_decision_recover(
    decision_id: Annotated[str, typer.Option()],
    inspection_outcome: Annotated[str, typer.Option(help="Typed inspection outcome.")],
    state_root: Annotated[Path, typer.Option()] = _DEFAULT_STATE_ROOT,
) -> None:
    """Record one bounded persisted-thread recovery inspection."""

    try:
        generation = _controller_decision_client(state_root).request_recovery(
            decision_id, inspection_outcome=inspection_outcome
        )
        typer.echo(
            json.dumps(
                {
                    "decision_id": str(generation.decision_id),
                    "generation": int(generation.generation),
                    "state": generation.state.value,
                },
                sort_keys=True,
            )
        )
    except (ControlClientError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _controller_generation_config(
    *,
    model: str,
    effort: ReasoningEffort,
    cwd: Path,
    effective_permission_json: str | None,
    native_compatibility_sha256: str | None,
) -> CodexSdkConfig:
    """Build a shared-session config from supervisor-bound permission facts."""

    if effective_permission_json is None and native_compatibility_sha256 is None:
        # Low-level provider-free fixtures may intentionally omit native
        # profile facts.  Keep that donor seam explicit and read-only.
        return CodexSdkConfig(model=model, reasoning_effort=effort, sandbox=Sandbox.READ_ONLY, cwd=cwd)
    if effective_permission_json is None or native_compatibility_sha256 is None:
        raise ValueError("controller native permission facts are incomplete")
    decoded = strict_json_loads(effective_permission_json, max_bytes=4096)
    if not isinstance(decoded, dict):
        raise ValueError("controller native permission facts are malformed")
    durable = NativePermissionAuthority.from_facts(decoded)
    home_value = os.environ.get("CODEX_HOME")
    native_home = Path(home_value or (Path.home() / ".codex")).resolve(strict=True)
    try:
        profile = NativeProfileProjection.load(native_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProfileFailure("controller native Codex profile cannot be revalidated") from exc
    if profile.worker_compatibility_sha256 != native_compatibility_sha256:
        raise ProfileFailure("controller native compatibility identity changed before SDK identity")
    try:
        profile.verify_worker_sources()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProfileFailure("controller native profile source changed before SDK identity") from exc
    current = profile.effective_authority(NativePermissionMode.INHERIT_NATIVE)
    effective = durable.meet(current)
    return CodexSdkConfig(
        model=model,
        reasoning_effort=effort,
        sandbox=None,
        cwd=cwd,
        native_runtime=NativeRuntimeConfig.shared(profile),
        permission_mode=effective.mode,
        effective_permission=effective,
    )


@app.command("controller-generation", hidden=True)
def controller_generation_service(
    decision_id: Annotated[str, typer.Option(help="Exact durable controller decision identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    cwd: Annotated[Path | None, typer.Option(help="Repository cwd selected by the supervisor.")] = None,
    model: Annotated[str, typer.Option(help="Explicit controller model.")] = "gpt-5.6-luna",
    reasoning_effort: Annotated[str, typer.Option(help="Explicit controller reasoning effort.")] = "medium",
    recover: Annotated[bool, typer.Option("--recover", help="Inspect the persisted generation once.")] = False,
    effective_permission_json: Annotated[str | None, typer.Option("--effective-permission-json", hidden=True)] = None,
    native_compatibility_sha256: Annotated[
        str | None, typer.Option("--native-compatibility-sha256", hidden=True)
    ] = None,
) -> None:
    """Private supervisor entrypoint for one bounded controller generation."""

    client = ControllerDecisionClient.for_state_root(state_root)
    selected_cwd = (cwd or state_root).resolve()
    try:
        effort = ReasoningEffort(reasoning_effort)
        adapter = CodexSdkAdapter(
            _controller_generation_config(
                model=model,
                effort=effort,
                cwd=selected_cwd,
                effective_permission_json=effective_permission_json,
                native_compatibility_sha256=native_compatibility_sha256,
            )
        )
        try:
            if recover:
                result = ControllerGenerationRecovery(client, adapter).recover(decision_id)
            else:
                result = ControllerGenerationRunner(
                    client,
                    adapter,
                    decision_id=decision_id,
                    claimant_id=f"controller-generation/{os.getpid()}",
                    cwd=selected_cwd,
                    model=model,
                    reasoning_effort=effort,
                ).run()
        finally:
            adapter.close()
        typer.echo(
            json.dumps(
                {
                    "decision_id": str(result.decision_id),
                    "generation": int(result.generation),
                    "outcome": result.outcome,
                    "action_id": result.action_id,
                    "detail": result.detail,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except ProfileFailure as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=WORKER_EXIT_PROFILE) from exc
    except (ControlClientError, ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command("program-controller-generation", hidden=True)
def program_controller_generation_service(
    decision_id: Annotated[str, typer.Option(help="Exact durable program decision identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    cwd: Annotated[Path | None, typer.Option(help="Repository cwd selected by the supervisor.")] = None,
    model: Annotated[str, typer.Option(help="Explicit ephemeral controller model.")] = "gpt-5.6-sol",
    reasoning_effort: Annotated[str, typer.Option(help="Explicit controller reasoning effort.")] = "medium",
    recover: Annotated[bool, typer.Option("--recover", help="Inspect the persisted generation once.")] = False,
    effective_permission_json: Annotated[str | None, typer.Option("--effective-permission-json", hidden=True)] = None,
    native_compatibility_sha256: Annotated[
        str | None, typer.Option("--native-compatibility-sha256", hidden=True)
    ] = None,
) -> None:
    """Private supervisor entrypoint for one ephemeral program decision generation."""

    client = ControllerDecisionClient.for_state_root(state_root)
    selected_cwd = (cwd or state_root).resolve()
    try:
        effort = ReasoningEffort(reasoning_effort)
        adapter = CodexSdkAdapter(
            _controller_generation_config(
                model=model,
                effort=effort,
                cwd=selected_cwd,
                effective_permission_json=effective_permission_json,
                native_compatibility_sha256=native_compatibility_sha256,
            )
        )
        try:
            if recover:
                result = ProgramControllerGenerationRecovery(client, adapter).recover(decision_id)
            else:
                result = ProgramControllerGenerationRunner(
                    client,
                    adapter,
                    decision_id=decision_id,
                    claimant_id=f"program-controller-generation/{os.getpid()}",
                    cwd=selected_cwd,
                    model=model,
                    reasoning_effort=effort,
                ).run()
        finally:
            adapter.close()
        typer.echo(
            json.dumps(
                {
                    "decision_id": str(result.decision_id),
                    "generation": int(result.generation),
                    "outcome": result.outcome,
                    "action_id": result.action_id,
                    "detail": result.detail,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except ProfileFailure as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=WORKER_EXIT_PROFILE) from exc
    except (ControlClientError, ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _register_hidden_diagnostic_aliases() -> None:
    """Keep pre-group root spellings callable without advertising duplicates."""

    for name, command in (
        ("schema-compatibility", schema_compatibility),
        ("worker-sentinel", worker_sentinel),
        ("sdk-compatibility-sentinel", sdk_compatibility_sentinel),
        ("controller-recovery-sentinel", controller_recovery_sentinel),
        ("live-control-sentinel", live_control_sentinel),
        ("review-pilot", review_pilot),
        ("multi-authority-review-pilot", multi_authority_review_pilot),
        ("workflow-control-pilot", workflow_control_pilot),
        ("production-pilots", production_pilots),
    ):
        app.command(name, hidden=True)(command)


_register_hidden_diagnostic_aliases()


if __name__ == "__main__":  # pragma: no cover
    app()
