"""CLI for deterministic adaptive-frontier scenarios."""

from __future__ import annotations

import json
from typing import Annotated, assert_never

import typer

from ctf_agent.frontier.demo import DemoScenario, false_candidate_demo

app = typer.Typer(help="Inspect deterministic adaptive-frontier behavior.")


@app.callback()
def frontier_cli() -> None:
    """Inspect deterministic adaptive-frontier behavior."""


@app.command("demo")
def demo(
    scenario: Annotated[
        DemoScenario, typer.Option("--scenario")
    ] = DemoScenario.FALSE_CANDIDATE,
    *,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a local scenario without models, Docker, or network access."""
    match scenario:
        case DemoScenario.FALSE_CANDIDATE:
            payload = false_candidate_demo()
        case unreachable:
            assert_never(unreachable)
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
