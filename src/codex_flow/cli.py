"""Agent- and human-facing CLI for the SDK-first workflow controller."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .app_native import AppNativeDispatchRecord, HostIdentity, HostingMode, HostReceipt
from .config import WorkflowConfigError
from .contracts import ModelFacingResult, model_facing_capsule_schema, model_facing_result_schema
from .controller import Controller, ControllerError, ResumeRequired, capsule_json, execution_json, load_capsule
from .controller_sentinel import run_controller_sentinel, write_controller_evidence
from .domain import CodexFlowError, ExecutionRecord, ExecutionStatus, ReasoningEffort, ThreadIdentity, strict_json_loads
from .h4_pilot import run_h4_multi_authority_pilot, run_h4_objective_pilot
from .h5_pilot import run_h5_medium_pilot, write_h5_evidence
from .h6_pilot import H6PilotError, app_native_visible_pilot_capsule, run_h6_pilots, write_h6_evidence
from .ledger import LedgerError, ledger_schema_compatibility
from .projection import load_control_capsule
from .sentinel import run_real_sentinel, write_evidence

app = typer.Typer(no_args_is_help=True, help="SDK-first Codex workflow tooling.")
_DEFAULT_STATE_ROOT = Path(".")


@app.callback()
def main() -> None:
    """SDK-first Codex workflow tooling."""


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


def _emit_app_native(record: AppNativeDispatchRecord, *, as_json: bool) -> None:
    payload = record.to_json()
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    else:
        typer.echo(f"{record.action.dispatch_id} state={record.state.value} recovery={record.recovery}")


@app.command("schema-compatibility")
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


@app.command("plan")
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
        _emit(method(run_id, milestone_id), as_json=as_json)
    except (ControllerError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()


@app.command("start")
def start_execution(
    run_id: Annotated[str, typer.Option(help="Durable run identity.")],
    milestone_id: Annotated[str, typer.Option(help="Durable milestone identity.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start exactly one planned SDK execution."""

    _execution_action("start", run_id, milestone_id, state_root, as_json)


@app.command("resume")
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
        Path,
        typer.Option(exists=True, dir_okay=False, help="Closed model-facing or internal execution capsule JSON."),
    ],
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
        parsed, _digest = load_control_capsule(capsule, state_root=state_root)
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


@app.command("app-bind")
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


@app.command("app-result")
def ingest_app_native_result(
    dispatch_id: Annotated[str, typer.Option(help="Exact bound logical dispatch identity.")],
    claim_token: Annotated[str, typer.Option(help="Capability returned only by App-native prepare.")],
    thread_id: Annotated[str, typer.Option(help="Exact bound native thread identity.")],
    host_id: Annotated[str, typer.Option(help="Exact bound hosting Codex app identity.")],
    result: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Closed ModelFacingResult JSON.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate and durably ingest one terminal result from the bound worker."""

    controller: Controller | None = None
    try:
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


@app.command("app-status")
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


@app.command("sdk-sentinel")
def sdk_sentinel(
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
    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_real_sentinel(
        model=model,
        effort=effort,
    )
    write_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("controller-sentinel")
def controller_sentinel(
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
    evidence = run_controller_sentinel(model=model, effort=effort)
    write_controller_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("h4-pilot")
def h4_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized H4-A pilot evidence path."),
    ] = Path("docs/reviews/evidence/h4-a-objective-pilot.json"),
) -> None:
    """Run one bounded objective/review/repair/fresh-review SDK pilot."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_h4_objective_pilot(model=model, effort=effort)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("h4b-pilot")
def h4b_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized H4-B pilot evidence path."),
    ] = Path("docs/reviews/evidence/h4-b-multi-authority-pilot.json"),
) -> None:
    """Run one bounded objective/visual/architecture SDK pilot."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_h4_multi_authority_pilot(model=model, effort=effort)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("h5-pilot")
def h5_pilot(
    real: Annotated[bool, typer.Option(help="Explicitly authorize the disposable real SDK pilot.")] = False,
    model: Annotated[str, typer.Option(help="Explicit model id; no default substitution.")] = ...,
    effort: Annotated[
        ReasoningEffort,
        typer.Option(help="Explicit reasoning effort; no default substitution."),
    ] = ...,
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized H5 evidence path."),
    ] = Path("docs/reviews/evidence/h5-workflow-control-medium.json"),
) -> None:
    """Run one bounded medium milestone through workflow-control/codex-flow."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    evidence = run_h5_medium_pilot(model=model, effort=effort)
    write_h5_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")
    if evidence["status"] != "passed":
        raise typer.Exit(code=1)


@app.command("h6-pilot")
def h6_pilot(
    real: Annotated[
        bool,
        typer.Option(help="Explicitly authorize the fixed medium and large real SDK pilots."),
    ] = False,
    config: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="Repository-owned workflow route configuration."),
    ] = Path("workflow.toml"),
    output: Annotated[
        Path,
        typer.Option(help="Retained sanitized H6 production-pilot evidence path."),
    ] = Path("docs/reviews/evidence/h6-production-pilots.json"),
) -> None:
    """Run the final integrated controller pilots and retirement decision."""

    if not real and os.environ.get("CODEX_FLOW_REAL_SDK") != "1":
        typer.echo("refusing real SDK start: pass --real or CODEX_FLOW_REAL_SDK=1")
        raise typer.Exit(code=2)
    try:
        evidence = run_h6_pilots(config_path=config.resolve())
    except (CodexFlowError, H6PilotError, WorkflowConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    write_h6_evidence(output, evidence)
    typer.echo(f"wrote {output}")
    typer.echo(f"status={evidence['status']}")


@app.command("h6-app-pilot-capsule")
def h6_app_pilot_capsule(
    parent_run_id: Annotated[str, typer.Option(help="Completed H6-C run that owns the existing workspace lease.")],
    state_root: Annotated[Path, typer.Option(help="Exact existing H6-C checkout.")] = _DEFAULT_STATE_ROOT,
    output: Annotated[
        Path | None,
        typer.Option(help="Optional exact controller-owned durable capsule path."),
    ] = None,
) -> None:
    """Plan and return the canonical durable capsule for the visible pilot."""

    controller: Controller | None = None
    try:
        capsule = app_native_visible_pilot_capsule(
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
    except (H6PilotError, TypeError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if controller is not None:
            controller.close()
    typer.echo(str(record.capsule_path))


if __name__ == "__main__":  # pragma: no cover
    app()
