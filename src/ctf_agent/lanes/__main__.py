"""Public inspection CLI for durable lane checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from ctf_agent.lanes.store import CorruptLaneCheckpointError, LaneCheckpointStore

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def main() -> None:
    """Inspect durable specialist lane state."""


@app.command()
def inspect(
    database: Annotated[Path, typer.Option("--database", exists=True, dir_okay=False)],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect all lane checkpoints in a state database."""
    try:
        checkpoints = LaneCheckpointStore(database).list()
    except CorruptLaneCheckpointError as exc:
        payload_error = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        if json_output:
            typer.echo(json.dumps(payload_error, sort_keys=True))
        typer.echo(f"{type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload: dict[str, Any] = {
        "database": str(database),
        "lanes": [item.model_dump(mode="json") for item in checkpoints],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    for checkpoint in checkpoints:
        typer.echo(
            f"{checkpoint.lane_id} generation={checkpoint.workspace_generation} "
            f"step={checkpoint.step_index} status={checkpoint.status.value}"
        )


if __name__ == "__main__":
    app()
