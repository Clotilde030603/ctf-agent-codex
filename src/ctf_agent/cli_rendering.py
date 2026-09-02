"""CLI settings validation and machine-readable run rendering."""

import json

import typer
from pydantic import ValidationError

from ctf_agent.config import Settings
from ctf_agent.schemas import RunRecord


def role_efforts(
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


def validated_settings(payload: dict[str, object]) -> Settings:
    try:
        return Settings.model_validate(payload)
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc


def render_run_result(result: RunRecord) -> None:
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "state": result.state,
                "run_dir": str(result.run_dir),
            }
        )
    )
    if result.last_error:
        raise typer.Exit(1)
