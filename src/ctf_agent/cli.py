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
    backend: Annotated[str, typer.Option("--backend")] = "codex",
    planner_model: Annotated[str | None, typer.Option("--planner-model")] = None,
    solver_model: Annotated[str | None, typer.Option("--solver-model")] = None,
    reviewer_model: Annotated[str | None, typer.Option("--reviewer-model")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    max_workers: Annotated[int, typer.Option("--max-workers", min=1, max=3)] = 3,
    auto_submit: Annotated[bool, typer.Option("--auto-submit")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    writeup: Annotated[bool, typer.Option("--writeup/--no-writeup")] = True,
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    allow_private_host: Annotated[bool, typer.Option("--allow-private-host")] = False,
    allow_local_reproduction: Annotated[
        bool, typer.Option("--allow-local-reproduction")
    ] = False,
    redact_flag: Annotated[bool, typer.Option("--redact-flag")] = False,
) -> None:
    """Create and execute a new challenge run."""
    if auto_submit and dry_run:
        raise typer.BadParameter("--auto-submit and --dry-run are mutually exclusive")
    defaults = Settings()
    settings = Settings.model_validate(
        {
            **defaults.model_dump(),
            "backend": backend,
            "planner_model": planner_model or defaults.planner_model,
            "solver_model": solver_model or defaults.solver_model,
            "verifier_model": reviewer_model or defaults.verifier_model,
            "planner_effort": reasoning_effort or defaults.planner_effort,
            "solver_effort": reasoning_effort or defaults.solver_effort,
            "verifier_effort": reasoning_effort or defaults.verifier_effort,
            "max_workers": max_workers,
            "runs_dir": runs_dir,
            "allow_private_hosts": allow_private_host,
            "allow_local_reproduction": allow_local_reproduction,
            "redact_flag": redact_flag,
        }
    )
    workflow = AutonomousWorkflow(settings)
    controller = workflow.controller()
    context = controller.create_run(
        url,
        auto_submit=auto_submit and not dry_run,
        writeup=writeup,
    )
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
