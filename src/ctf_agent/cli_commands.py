"""Operational CLI commands and their output rendering."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ctf_agent.benchmark import benchmark as run_benchmark
from ctf_agent.cli_rendering import render_run_result, validated_settings
from ctf_agent.config import Settings
from ctf_agent.doctor import run_doctor
from ctf_agent.workflow import AutonomousWorkflow


def retry_evidence(
    run_id: Annotated[str, typer.Argument(help="Accepted run identifier")],
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    challenge_url: Annotated[
        str | None,
        typer.Option(
            "--challenge-url",
            help="Re-supply a credential-bearing URL without persisting its secret query",
        ),
    ] = None,
) -> None:
    """Retry missing evidence for a durably Accepted run without resubmitting."""
    workflow = AutonomousWorkflow.from_run(runs_dir, run_id)
    controller = workflow.controller()
    context = controller.retry_evidence(run_id, challenge_url=challenge_url)
    render_run_result(asyncio.run(controller.execute(context)))


def benchmark_command(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    ablation_matrix: Annotated[
        Path | None,
        typer.Option("--ablation-matrix", exists=True, dir_okay=False),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", dir_okay=False),
    ] = None,
    solve_k: Annotated[int, typer.Option("--solve-k", min=1)] = 3,
) -> None:
    """Run an offline benchmark manifest or a frozen paired ablation."""
    if ablation_matrix is None:
        rendered = json.dumps(run_benchmark(manifest), indent=2, sort_keys=True) + "\n"
    else:
        from ctf_agent.ablation_report import canonical_report_json
        from ctf_agent.ablation_runner import ablation_benchmark

        try:
            rendered = canonical_report_json(
                ablation_benchmark(manifest, ablation_matrix, solve_k=solve_k)
            )
        except (ValidationError, ValueError, OSError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.write_text(rendered, encoding="utf-8")


def doctor(
    runs_dir: Annotated[Path, typer.Option("--runs-dir")] = Path("runs"),
    backend: Annotated[str | None, typer.Option("--backend")] = None,
    docker_image: Annotated[str | None, typer.Option("--docker-image")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check runtime, authentication, Docker daemon, tools, and browser readiness."""
    defaults = Settings()
    settings = validated_settings(
        {
            **defaults.model_dump(),
            "runs_dir": runs_dir,
            "backend": backend or defaults.backend,
            "docker_image": docker_image or defaults.docker_image,
        }
    )
    report = run_doctor(settings)
    payload = report.model_dump(mode="json") | {"ok": report.ok}
    typer.echo(json.dumps(payload, indent=2 if json_output else None))
    if not report.ok:
        raise typer.Exit(1)
