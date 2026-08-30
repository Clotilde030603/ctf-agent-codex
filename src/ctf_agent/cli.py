"""Command-line entry point."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ctf_agent.benchmark import benchmark as run_benchmark
from ctf_agent.config import Settings
from ctf_agent.workflow import AutonomousWorkflow

app = typer.Typer(no_args_is_help=True, help="Deterministic autonomous CTF agent")


def _role_efforts(
    reasoning_effort: str | None,
    planner_effort: str | None,
    solver_effort: str | None,
    reviewer_effort: str | None,
    defaults: Settings,
) -> dict[str, str]:
    return {
        "planner_effort": planner_effort or reasoning_effort or defaults.planner_effort,
        "solver_effort": solver_effort or reasoning_effort or defaults.solver_effort,
        "verifier_effort": reviewer_effort or reasoning_effort or defaults.verifier_effort,
    }


def _validated_settings(payload: dict[str, object]) -> Settings:
    try:
        return Settings.model_validate(payload)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def solve(
    url: Annotated[str, typer.Argument(help="CTF challenge URL")],
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
) -> None:
    """Create and execute a new challenge run."""
    if auto_submit and dry_run:
        raise typer.BadParameter("--auto-submit and --dry-run are mutually exclusive")
    defaults = Settings()
    settings = _validated_settings(
        {
            **defaults.model_dump(),
            "backend": backend,
            "planner_model": planner_model or defaults.planner_model,
            "solver_model": solver_model or defaults.solver_model,
            "verifier_model": reviewer_model or defaults.verifier_model,
            **_role_efforts(
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
    backend: Annotated[str | None, typer.Option("--backend")] = None,
    planner_model: Annotated[str | None, typer.Option("--planner-model")] = None,
    solver_model: Annotated[str | None, typer.Option("--solver-model")] = None,
    reviewer_model: Annotated[str | None, typer.Option("--reviewer-model")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    planner_effort: Annotated[str | None, typer.Option("--planner-effort")] = None,
    solver_effort: Annotated[str | None, typer.Option("--solver-effort")] = None,
    reviewer_effort: Annotated[str | None, typer.Option("--reviewer-effort")] = None,
    model_timeout: Annotated[float | None, typer.Option("--model-timeout")] = None,
    model_call_budget: Annotated[int | None, typer.Option("--model-call-budget")] = None,
    max_workers: Annotated[int | None, typer.Option("--max-workers", min=1, max=3)] = None,
    allow_private_host: Annotated[
        bool | None,
        typer.Option("--allow-private-host/--disallow-private-host"),
    ] = None,
    allow_local_reproduction: Annotated[
        bool | None,
        typer.Option("--allow-local-reproduction/--require-docker-reproduction"),
    ] = None,
    redact_flag: Annotated[
        bool | None,
        typer.Option("--redact-flag/--show-flag"),
    ] = None,
    docker_image: Annotated[str | None, typer.Option("--docker-image")] = None,
) -> None:
    """Continue from the last durable state checkpoint."""
    overrides: dict[str, object] = {
        key: value
        for key, value in {
            "backend": backend,
            "planner_model": planner_model,
            "solver_model": solver_model,
            "verifier_model": reviewer_model,
            "model_timeout_seconds": model_timeout,
            "model_call_budget": model_call_budget,
            "max_workers": max_workers,
            "allow_private_hosts": allow_private_host,
            "allow_local_reproduction": allow_local_reproduction,
            "redact_flag": redact_flag,
            "docker_image": docker_image,
        }.items()
        if value is not None
    }
    if any(
        value is not None
        for value in (
            reasoning_effort,
            planner_effort,
            solver_effort,
            reviewer_effort,
        )
    ):
        snapshot_workflow = AutonomousWorkflow.from_run(runs_dir, run_id)
        overrides.update(
            _role_efforts(
                reasoning_effort,
                planner_effort,
                solver_effort,
                reviewer_effort,
                snapshot_workflow.settings,
            )
        )
    try:
        workflow = AutonomousWorkflow.from_run(runs_dir, run_id, overrides=overrides)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
