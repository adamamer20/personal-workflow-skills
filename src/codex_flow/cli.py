"""Small human-facing CLI for H1 compatibility checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from .domain import ReasoningEffort
from .sentinel import run_real_sentinel, write_evidence

app = typer.Typer(no_args_is_help=True, help="SDK-first Codex workflow tooling.")


@app.callback()
def main() -> None:
    """SDK-first Codex workflow tooling."""


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


if __name__ == "__main__":  # pragma: no cover
    app()
