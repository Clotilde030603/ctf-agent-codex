"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow


from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.evidence import EvidenceManifest
from ctf_agent.reproduction import reproduce_solver
from ctf_agent.schemas import (
    RunState,
)
from ctf_agent.writeup import WriteupGenerator, WriteupValidator


async def writeup(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    generator = WriteupGenerator()
    outputs = generator.generate_all(
        context.record.run_dir,
        redact_flags=workflow.settings.redact_flag,
    )
    validation = WriteupValidator().validate_all(context.record.run_dir)
    if not validation.ok:
        context.ledger.append(
            context.record.run_id,
            "writeup.validated",
            {"accepted": False, "errors": validation.errors},
            state=context.record.state.value,
        )
        raise RuntimeError("write-up validation failed: " + "; ".join(validation.errors))
    context.ledger.append(
        context.record.run_id,
        "writeup.validated",
        {"accepted": True},
        state=context.record.state.value,
    )
    manifest_path = context.record.run_dir / "evidence" / "manifest.json"
    warnings = len(EvidenceManifest.load(manifest_path).failures) if manifest_path.is_file() else 1
    return StateOutcome(
        RunState.DONE_WITH_WARNINGS if warnings else RunState.DONE,
        {
            "writeup_markdown": str(outputs.markdown_path),
            "writeup_html": str(outputs.html_path),
            "provenance": str(outputs.provenance_path),
            "redact_flag": workflow.settings.redact_flag,
        },
    )


async def reproduce(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    result = await reproduce_solver(
        context.record.run_dir,
        workflow._candidate(context).value,
        spec=workflow._candidate(context).reproduction_spec,
        image=workflow.settings.docker_image,
        timeout_seconds=workflow.settings.tool_timeout_seconds,
        use_docker=not workflow.settings.allow_local_reproduction,
    )
    context.ledger.append(
        context.record.run_id,
        "solver.reproduced",
        {
            "accepted": result.success,
            "flag": workflow._candidate(context).value,
            "exit_code": result.exit_code,
            "command": result.command,
        },
        state=RunState.REPRODUCE.value,
    )
    if not result.success:
        if result.command and result.command[0] == "docker":
            raise RuntimeError(
                "clean Docker reproduction failed before submission: "
                + (result.stderr or f"exit code {result.exit_code}")
            )
        return StateOutcome(RunState.SOLVE, {"reproduced": False})
    return StateOutcome(RunState.SUBMIT, {"reproduced": True})
