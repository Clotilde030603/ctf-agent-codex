"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from ctf_agent.benchmark import benchmark as run_benchmark
from ctf_agent.config import Settings
from ctf_agent.workflow import AutonomousWorkflow

app = typer.Typer(no_args_is_help=True, help="Deterministic autonomous CTF agent")


@app.command()
def solve(
    url: Annotated[str, typer.Argument(help="CTF challenge URL")],
    auto_submit: Annotated[bool, typer.Option("--auto-submit")] = False,
    writeup: Annotated[bool, typer.Option("--writeup/--no-writeup")] = True,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    allow_private_host: Annotated[bool, typer.Option("--allow-private-host")] = False,
    allow_local_reproduction: Annotated[
        bool, typer.Option("--allow-local-reproduction")
    ] = False,
) -> None:
    """Create and execute a new challenge run."""
    settings = Settings(
        runs_dir=runs_dir,
        allow_private_hosts=allow_private_host,
        allow_local_reproduction=allow_local_reproduction,
    )
    workflow = AutonomousWorkflow(settings)
    controller = workflow.controller()
    context = controller.create_run(url, auto_submit=auto_submit, writeup=writeup)
    result = asyncio.run(controller.execute(context))
    typer.echo(
        json.dumps(
            {"run_id": result.run_id, "state": result.state, "run_dir": str(result.run_dir)}
        )
    )
    if result.last_error:
        raise typer.Exit(1)


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    challenge_url: Annotated[
        str | None,
        typer.Option(
            "--challenge-url",
            help="Re-supply a credential-bearing URL without persisting its secret query",
        ),
    ] = None,
) -> None:
    """Continue from the last durable state checkpoint."""
    workflow = AutonomousWorkflow(Settings(runs_dir=runs_dir))
    controller = workflow.controller()
    context = controller.resume_run(run_id, challenge_url=challenge_url)
    result = asyncio.run(controller.execute(context))
    typer.echo(
        json.dumps(
            {"run_id": result.run_id, "state": result.state, "run_dir": str(result.run_dir)}
        )
    )
    if result.last_error:
        raise typer.Exit(1)


@app.command("benchmark")
def benchmark_command(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Run an offline benchmark manifest."""
    typer.echo(json.dumps(run_benchmark(manifest), indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
