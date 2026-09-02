"""CLI command for resuming a durable challenge run."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ctf_agent.cli_rendering import render_run_result, role_efforts
from ctf_agent.workflow import AutonomousWorkflow


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
    approve_static_submit: Annotated[
        bool | None,
        typer.Option("--approve-static-submit/--revoke-static-submit-approval"),
    ] = None,
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
            "approve_static_submission": approve_static_submit,
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
            role_efforts(
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
    render_run_result(asyncio.run(controller.execute(context)))
