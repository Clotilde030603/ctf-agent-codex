"""Machine-readable ContextProjector command-line surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ConfigDict, Field

from ctf_agent.context_projector import ContextProjector, render_codex_prompt
from ctf_agent.models.base import ModelBackendError, ModelRequest


class ProjectionCliInput(BaseModel):
    """Validated request envelope accepted by the projection CLI."""

    model_config = ConfigDict(frozen=True)

    role: str = "planner"
    system: str | None = None
    developer: str | None = None
    task: str = "Project this authorized CTF context."
    context: dict[str, Any] = Field(default_factory=dict)


def main(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
    budget: Annotated[int, typer.Option("--budget", min=1)],
    output: Annotated[Path, typer.Option("--output", dir_okay=False)],
) -> None:
    """Project one JSON request and write prompt, context, and manifest JSON."""
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("input must be a JSON object", param_hint="--input")
    if "context" not in raw:
        raw = {"context": raw}
    value = ProjectionCliInput.model_validate(raw)
    request = ModelRequest(
        role=value.role,
        system=value.system,
        developer=value.developer,
        prompt=value.task,
        context=value.context,
    )
    try:
        projection = ContextProjector(budget).project(request, render_codex_prompt)
    except ModelBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "manifest": projection.manifest.model_dump(mode="json"),
        "projected_context": [section.model_dump(mode="json") for section in projection.sections],
        "rendered_prompt": projection.rendered,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    typer.echo(json.dumps(projection.manifest.model_dump(mode="json"), sort_keys=True))


if __name__ == "__main__":
    typer.run(main)
