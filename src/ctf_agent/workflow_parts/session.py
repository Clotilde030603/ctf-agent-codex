"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow

from typing import Any, cast
from urllib.parse import urlsplit

from ctf_agent.engine import RunContext, StateOutcome
from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.platforms.detect import create_detected_adapter
from ctf_agent.schemas import (
    RunState,
)
from ctf_agent.scope import HostScope
from ctf_agent.triage import ScanConfig, classify_report, scan_path
from ctf_agent.workflow_parts.io import _write_json


async def _adapter(workflow: AutonomousWorkflow, context: RunContext) -> PlatformAdapter:
    if workflow._adapter_override is not None:
        return workflow._adapter_override
    adapter = context.values.get("adapter")
    if adapter is not None:
        return cast(PlatformAdapter, adapter)
    parsed = urlsplit(workflow._challenge_url(context))
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    storage_state = workflow.settings.browser_storage_state
    if storage_state is None:
        storage_state = workflow.settings.runs_dir / ".sessions" / f"{parsed.hostname}.json"

    def observe(request: Any) -> None:
        context.ledger.append(
            context.record.run_id,
            "network.request",
            dict(request),
            state=context.record.state.value,
        )

    scope = HostScope.from_url(
        base_url,
        allow_private_hosts=workflow.settings.allow_private_hosts,
    )
    session = ScopedAsyncSession(
        scope,
        config=SessionConfig(
            timeout_seconds=workflow.settings.request_timeout_seconds,
            retry_budget=workflow.settings.retry_budget,
            rate_limit_per_second=workflow.settings.rate_limit_per_second,
        ),
        request_observer=observe,
    )
    created = await create_detected_adapter(
        workflow._challenge_url(context),
        session=session,
        browser_storage_state=storage_state,
        allow_private_hosts=workflow.settings.allow_private_hosts,
    )
    context.values["adapter"] = created
    return created


async def authenticate(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    return_state = context.store.reauthentication_target(context.record.run_id)
    adapter = await workflow._adapter(context)
    session = await adapter.authenticate()
    adapter_session = getattr(adapter, "session", None)
    if not session.authenticated:
        return StateOutcome(
            RunState.NEEDS_AUTHENTICATION,
            {"authenticated": False, "user_action_required": True},
            error="needs_authentication: user action required",
        )
    if isinstance(adapter_session, ScopedAsyncSession):
        adapter_session.mark_authenticated(workflow._challenge_url(context))
        context.values["auth_handle"] = workflow._auth_broker.register(adapter_session)
        workflow._runtime_capabilities = None
    elif return_state is not None:
        return StateOutcome(
            RunState.NEEDS_AUTHENTICATION,
            {"authenticated": False, "user_action_required": True},
            error="needs_authentication: user action required",
        )
    workflow._resume_auth_required = False
    context.ledger.append(
        context.record.run_id,
        "auth.ready",
        {"status": "available", "return_state": return_state.value if return_state else None},
        state=RunState.AUTHENTICATE.value,
    )
    return StateOutcome(
        return_state or RunState.INGEST,
        {"authenticated": True, "session_handle": "opaque"},
    )


async def ingest(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    adapter = await workflow._adapter(context)
    challenge = await adapter.fetch_challenge(workflow._challenge_url(context))
    challenge.flag_policy = await adapter.extract_flag_policy(challenge)
    artifacts = await adapter.download_attachments(challenge, context.record.run_dir / "files")
    _write_json(context.record.run_dir / "challenge.json", challenge.model_dump(mode="json"))
    context.values.update(challenge=challenge, artifacts=artifacts)
    context.ledger.append(
        context.record.run_id,
        "network.ingest",
        {
            "challenge_url": challenge.url,
            "attachment_urls": challenge.attachment_urls,
            "artifact_count": len(artifacts),
        },
        state=RunState.INGEST.value,
    )
    return StateOutcome(RunState.TRIAGE, {"artifacts": len(artifacts)})


async def triage(workflow: AutonomousWorkflow, context: RunContext) -> StateOutcome:
    config = ScanConfig(
        max_depth=workflow.settings.max_extraction_depth,
        max_total_extracted_size=workflow.settings.max_extracted_bytes,
        tool_timeout_seconds=workflow.settings.tool_timeout_seconds,
    )
    report = scan_path(
        context.record.run_dir / "files",
        context.record.run_dir / "artifacts" / "triage",
        config,
    )
    classification = classify_report(report)
    payload = report.to_dict()
    payload["classification"] = classification.to_dict()
    _write_json(context.record.run_dir / "triage.json", payload)
    context.values.update(triage=payload, classification=classification)
    return StateOutcome(
        RunState.PLAN,
        {
            "files": len(report.files),
            "primary_category": classification.primary_category,
        },
    )
