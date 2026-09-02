"""Command-line entry point and public command facade."""

import typer

from ctf_agent.cli_commands import benchmark_command, doctor, retry_evidence
from ctf_agent.cli_rendering import role_efforts, validated_settings
from ctf_agent.cli_resume import resume
from ctf_agent.cli_solve import solve

app = typer.Typer(no_args_is_help=True, help="Deterministic autonomous CTF agent")
app.command()(solve)
app.command()(resume)
app.command("retry-evidence")(retry_evidence)
app.command("benchmark")(benchmark_command)
app.command()(doctor)

_role_efforts = role_efforts
_validated_settings = validated_settings

__all__ = [
    "app",
    "benchmark_command",
    "doctor",
    "resume",
    "retry_evidence",
    "solve",
]

if __name__ == "__main__":
    app()
