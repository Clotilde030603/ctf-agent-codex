"""CLI command for creating a challenge run."""

import asyncio
from pathlib import Path
from typing import Annotated

import click
import typer

from ctf_agent.cli_rendering import render_run_result, role_efforts, validated_settings
from ctf_agent.config import Settings
from ctf_agent.workflow import AutonomousWorkflow


def solve(
    url: Annotated[str | None, typer.Argument(help="CTF challenge URL")] = None,
    backend: Annotated[str, typer.Option("--backend")] = "codex",
    planner_model: Annotated[str | None, typer.Option("--planner-model")] = None,
    solver_model: Annotated[str | None, typer.Option("--solver-model")] = None,
    reviewer_model: Annotated[str | None, typer.Option("--reviewer-model")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    planner_effort: Annotated[str | None, typer.Option("--planner-effort")] = None,
    solver_effort: Annotated[str | None, typer.Option("--solver-effort")] = None,
    reviewer_effort: Annotated[str | None, typer.Option("--reviewer-effort")] = None,
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
    approve_static_submit: Annotated[
        bool,
        typer.Option(
            "--approve-static-submit",
            help="Explicitly permit static-backend submission without model consensus",
        ),
    ] = False,
) -> None:
    """Create and execute a new challenge run."""
    if url is None:
        raise click.UsageError("Missing argument 'URL'.")
    if auto_submit and dry_run:
        raise typer.BadParameter("--auto-submit and --dry-run are mutually exclusive")
    defaults = Settings()
    settings = validated_settings(
        {
            **defaults.model_dump(),
            "backend": backend,
            "planner_model": planner_model or defaults.planner_model,
            "solver_model": solver_model or defaults.solver_model,
            "verifier_model": reviewer_model or defaults.verifier_model,
            **role_efforts(
                reasoning_effort,
                planner_effort,
                solver_effort,
                reviewer_effort,
                defaults,
            ),
            "max_workers": max_workers,
            "runs_dir": runs_dir,
            "allow_private_hosts": allow_private_host,
            "allow_local_reproduction": allow_local_reproduction,
            "redact_flag": redact_flag,
            "approve_static_submission": approve_static_submit,
        }
    )
    workflow = AutonomousWorkflow(settings)
    controller = workflow.controller()
    context = controller.create_run(
        url,
        auto_submit=auto_submit and not dry_run,
        writeup=writeup,
    )
    render_run_result(asyncio.run(controller.execute(context)))
