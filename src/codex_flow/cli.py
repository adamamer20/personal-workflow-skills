"""Agent- and human-facing CLI for the SDK-first workflow controller."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .contracts import model_facing_capsule_schema, model_facing_result_schema
from .controller import Controller, ControllerError, ResumeRequired, execution_json, load_capsule
from .controller_sentinel import run_controller_sentinel, write_controller_evidence
from .domain import ExecutionRecord, ExecutionStatus, ReasoningEffort
from .h4_pilot import run_h4_multi_authority_pilot, run_h4_objective_pilot
from .h5_pilot import run_h5_medium_pilot, write_h5_evidence
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
    capsule: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Typed execution capsule JSON.")],
    state_root: Annotated[Path, typer.Option(help="Checkout that owns .codex-flow state.")] = _DEFAULT_STATE_ROOT,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Explicitly resume an existing durable SDK identity."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit stable machine-readable JSON.")] = False,
) -> None:
    """Plan once, start once, and report the controller's durable status.

    This is the packaged agent entrypoint.  It does not create native peer
    tasks or retry an uncertain external call.  Use ``--resume`` only after a
    prior status explicitly reports a durable SDK identity.
    """

    controller = _controller(state_root)
    try:
        parsed, _digest = load_capsule(capsule)
        try:
            planned = controller.plan(parsed)
        except ControllerError as planning_error:
            # Only a terminal existing plan may be reconciled read-only.  Do
            # not hide a conflicting capsule, scope violation, or integrity
            # failure behind a status lookup.
            existing = controller.status(parsed.run_id, parsed.milestone_id)
            if existing.status not in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
                raise planning_error
            planned = existing
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


if __name__ == "__main__":  # pragma: no cover
    app()
