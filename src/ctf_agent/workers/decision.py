"""Model decision requests and decision provenance tracking."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ctf_agent.models.base import ModelBackendError, ModelRequest
from ctf_agent.skills import SkillSelection
from ctf_agent.workers.models import WorkerDecision, WorkerReport

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore


async def next_decision(
    worker: WorkerCore,
    task: str,
    context: dict[str, Any],
    reports: list[WorkerReport],
) -> WorkerDecision:
    started = time.monotonic()
    selection = context.get("_skill_selection")
    skills = selection if isinstance(selection, SkillSelection) else None
    model_context = {
        key: value for key, value in context.items() if key != "_skill_selection"
    }
    response = await worker.backend.complete(
        ModelRequest(
            role="solver",
            system=(
                "You are a sandboxed CTF worker. Return one JSON object matching the "
                "WorkerDecision schema. Never return shell strings; use argv arrays only. "
                "Use http_request only for explicitly scoped challenge URLs; credentials "
                "are supplied by the host session and must never be placed in headers."
            ),
            prompt=task,
            context={
                **model_context,
                "workspace": str(worker.workspace.root),
                "recent_reports": [
                    report.model_dump(mode="json") for report in reports[-5:]
                ],
            },
            output_schema=WorkerDecision.model_json_schema(),
            developer=skills.developer_instructions if skills is not None else None,
            skill_runtime=skills.runtime if skills is not None else None,
        )
    )
    projection_manifest = response.metadata.get("projection_manifest")
    emit(
        worker,
        "model.completed",
        {
            "role": "solver",
            "report_count": len(reports),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            **(
                {"projection_manifest": projection_manifest}
                if isinstance(projection_manifest, dict)
                else {}
            ),
        },
    )
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError as exc:
        raise ModelBackendError("worker decision must be valid JSON") from exc
    return WorkerDecision.model_validate(payload)


def capture_decision_progress(worker: WorkerCore, decision: WorkerDecision) -> bool:
    progressed = False
    for fact in decision.facts:
        if fact not in worker._seen_facts:
            worker._seen_facts.add(fact)
            progressed = True
    for candidate in decision.flag_candidates:
        key = hashlib.sha256(candidate.value.encode()).hexdigest()
        if key not in worker._seen_candidates:
            worker._seen_candidates.add(key)
            emit(
                worker,
                "flag.candidate",
                {
                    "candidate_sha256": hashlib.sha256(
                        candidate.value.encode()
                    ).hexdigest(),
                    "source_artifact": candidate.source_artifact,
                    "source_location": candidate.source_location,
                    "confidence": candidate.confidence,
                },
            )
            progressed = True
    return progressed


def emit(worker: WorkerCore, event_type: str, payload: Mapping[str, Any]) -> None:
    if worker.event_observer is not None:
        worker.event_observer(event_type, payload)
