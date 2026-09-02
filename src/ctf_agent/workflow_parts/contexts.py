"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

from pathlib import Path
from typing import Any

from ctf_agent.capabilities import (
    RuntimeCapabilitySnapshot,
)
from ctf_agent.engine import RunContext
from ctf_agent.models.base import ModelBackend
from ctf_agent.models.factory import create_codex_backend
from ctf_agent.skills import SkillRegistry, SkillSelection
from ctf_agent.specialists.crypto import CryptoSpecialist
from ctf_agent.specialists.forensics import ForensicsSpecialist
from ctf_agent.specialists.toolchain import PWN_PROFILE, REV_PROFILE, ToolchainSpecialist
from ctf_agent.specialists.web import StaticWebSpecialist


def _planner_backend(workflow: AutonomousWorkflow, context: RunContext, role: str) -> ModelBackend:
    if workflow._planner_backend_override is not None:
        return workflow._planner_backend_override
    return create_codex_backend(workflow.settings, role, context.record.run_dir)


def _planning_context(
    workflow: AutonomousWorkflow, context: RunContext, triage_data: dict[str, Any]
) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for item in triage_data.get("files", [])[:100]:
        if not isinstance(item, dict):
            continue
        files.append(
            {
                key: item.get(key)
                for key in (
                    "relative_path",
                    "size",
                    "sha256",
                    "mime",
                    "magic",
                    "entropy",
                    "language",
                    "parent_archive",
                    "extraction_depth",
                )
            }
            | {
                "indicators": item.get("indicators", [])[:50],
                "tool_results": item.get("tool_results", [])[:20],
            }
        )
    previous_events = [
        {
            "type": event["event_type"],
            "state": event.get("state"),
            "payload": event["payload"],
        }
        for event in context.ledger.list(context.record.run_id)
        if event["event_type"]
        in {
            "state.error",
            "flag.verification_failed",
            "flag.submitted",
            "model.failure",
            "solve.round",
        }
    ][-20:]
    challenge = workflow._challenge(context)
    runtime_capabilities = workflow._runtime_capability_snapshot(context.record.run_dir)
    return {
        "run_id": context.record.run_id,
        "challenge": challenge.model_dump(mode="json"),
        "flag_policy": challenge.flag_policy.model_dump(mode="json"),
        "service_hosts": challenge.service_hosts,
        "classification": triage_data.get("classification", {}),
        "files": files,
        "previous_attempts_and_failures": previous_events,
        "runtime_capabilities": runtime_capabilities.prompt_context(),
    }


def _solver_context(
    workflow: AutonomousWorkflow, context: RunContext, triage_data: object
) -> dict[str, object]:
    planning = workflow._planning_context(
        context, triage_data if isinstance(triage_data, dict) else {}
    )
    classification = planning.get("classification")
    category = (
        str(classification.get("primary_category", "misc"))
        if isinstance(classification, dict)
        else "misc"
    )
    return {
        **planning,
        "run_dir": str(context.record.run_dir),
        "state_database": str(context.store.database),
        "triage": triage_data,
        "auth_handle": context.values.get("auth_handle"),
        "auth_session_status": workflow._runtime_capability_snapshot()
        .require("auth:http-session")
        .status.value,
        "_skill_selection": workflow._skill_selection(context, category),
    }


def _skill_selection(
    workflow: AutonomousWorkflow, context: RunContext, category: str
) -> SkillSelection:
    current = context.values.get("skill_selection")
    if isinstance(current, SkillSelection):
        return current
    selected = SkillRegistry.repository().select(
        category,
        runtime_capabilities=workflow._runtime_capability_snapshot(context.record.run_dir),
    )
    selected.write(context.record.run_dir / "artifacts" / "runtime-skills.json")
    context.values["skill_selection"] = selected
    context.ledger.append(
        context.record.run_id,
        "skills.selected",
        {
            "selected_skills": [item.model_dump(mode="json") for item in selected.identities],
            "tool_routing": selected.tool_routing.model_dump(mode="json"),
        },
        state=context.record.state.value,
        idempotency_key="skills-selected",
    )
    return selected


def _category_specialist(
    triage_data: object,
    runtime_capabilities: RuntimeCapabilitySnapshot | None = None,
) -> Any:
    if not isinstance(triage_data, dict):
        return None
    classification = triage_data.get("classification", {})
    if not isinstance(classification, dict):
        return None
    primary = str(classification.get("primary_category", "")).lower()
    if primary in {"crypto-math", "crypto-binary"}:
        return CryptoSpecialist()
    if primary in {"forensics", "misc"}:
        return ForensicsSpecialist()
    if primary == "web":
        return StaticWebSpecialist()
    if primary == "rev" and runtime_capabilities is not None:
        return ToolchainSpecialist(REV_PROFILE, runtime_capabilities)
    if primary == "pwn" and runtime_capabilities is not None:
        return ToolchainSpecialist(PWN_PROFILE, runtime_capabilities)
    return None


def _runtime_capability_snapshot(
    workflow: AutonomousWorkflow, run_dir: Path | None = None
) -> RuntimeCapabilitySnapshot:
    if workflow._runtime_capabilities is None:
        allowed_tools: frozenset[str] | None = (
            None if workflow.settings.runtime_capability_mode == "corrected" else frozenset()
        )
        workflow._runtime_capabilities = workflow._capability_provider.snapshot(
            workflow.settings.docker_image,
            allowed_tools=allowed_tools,
            authenticated_session=workflow._auth_broker.available,
        )
    if run_dir is not None:
        workflow._runtime_capabilities.write(run_dir / "artifacts" / "runtime-capabilities.json")
    return workflow._runtime_capabilities
