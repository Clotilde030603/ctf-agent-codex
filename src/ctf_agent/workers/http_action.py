"""Scoped HTTP action execution and multipart confinement."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ctf_agent.security import protect_file
from ctf_agent.workers.artifacts import findings_to_dict, truncate
from ctf_agent.workers.command import command_fingerprint
from ctf_agent.workers.models import WorkerDecision, WorkerExecutionError, WorkerReport

if TYPE_CHECKING:
    from ctf_agent.workers.core import WorkerCore


async def http_request(
    worker: WorkerCore, step: int, decision: WorkerDecision
) -> WorkerReport:
    if worker.http_session is None:
        raise WorkerExecutionError("scoped HTTP access is not configured for this lane")
    assert decision.method is not None
    assert decision.url is not None
    serialized_body = json.dumps(
        {
            "body": decision.body,
            "json_body": decision.json_body,
            "form_body": decision.form_body,
            "multipart": [item.model_dump() for item in decision.multipart],
        },
        sort_keys=True,
        default=str,
    )
    if len(serialized_body.encode("utf-8")) > 256_000:
        raise WorkerExecutionError("HTTP request metadata/body exceeds 256000 bytes")
    fingerprint = command_fingerprint(
        [
            decision.method,
            decision.url,
            json.dumps(decision.headers, sort_keys=True),
            json.dumps(decision.query_params, sort_keys=True),
            serialized_body,
        ],
        workspace_generation=worker._workspace_generation,
    )
    if fingerprint in worker._seen_commands:
        progress = worker._capture_decision_progress(decision)
        return WorkerReport(
            step=step,
            action="http_request",
            status="skipped",
            message="duplicate HTTP request fingerprint",
            method=decision.method,
            url=decision.url,
            command_fingerprint=fingerprint,
            facts=decision.facts,
            flag_candidates=decision.flag_candidates,
            made_progress=progress,
        )
    request_kwargs: dict[str, Any] = {
        "headers": decision.headers,
        "params": decision.query_params,
    }
    if decision.body is not None:
        request_kwargs["content"] = decision.body
    elif decision.json_body is not None:
        request_kwargs["json"] = decision.json_body
    elif decision.form_body:
        request_kwargs["data"] = decision.form_body
    elif decision.multipart:
        request_kwargs["files"] = [
            (
                upload.field_name,
                (
                    upload.filename or Path(upload.path).name,
                    read_upload(worker, upload.path),
                    upload.content_type,
                ),
            )
            for upload in decision.multipart
        ]
    try:
        response = await worker.http_session.request(
            decision.method,
            decision.url,
            **request_kwargs,
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise WorkerExecutionError(f"scoped HTTP request failed: {exc}") from exc
    worker._seen_commands.add(fingerprint)
    body = worker.sanitizer.sanitize(
        truncate(response.content, worker.budget.stdout_limit)
    )
    artifact_prefix = f"{step:03d}-{fingerprint[:16]}"
    response_path = worker.workspace.artifacts_dir / f"{artifact_prefix}.http.txt"
    metadata_path = worker.workspace.artifacts_dir / f"{artifact_prefix}.http.json"
    response_path.write_text(body.text, encoding="utf-8")
    protect_file(response_path)
    safe_headers: dict[str, str] = {}
    header_bytes = 0
    for name, value in response.headers.items():
        if name.lower() in {"set-cookie", "authorization", "proxy-authenticate"}:
            continue
        sanitized_value = worker.sanitizer.sanitize(value).text
        item_size = len(name.encode()) + len(sanitized_value.encode())
        if header_bytes + item_size > worker.budget.response_header_limit:
            break
        safe_headers[name] = sanitized_value
        header_bytes += item_size
    chain_value = response.extensions.get("ctf_redirect_chain", [])
    redirect_chain = (
        [dict(item) for item in chain_value if isinstance(item, Mapping)]
        if isinstance(chain_value, list)
        else []
    )
    metadata_path.write_text(
        json.dumps(
            {
                "method": decision.method,
                "url": str(response.request.url),
                "status_code": response.status_code,
                "headers": safe_headers,
                "redirect_chain": redirect_chain,
                "fingerprint": fingerprint,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    protect_file(metadata_path)
    report = WorkerReport(
        step=step,
        action="http_request",
        status="ok",
        message=decision.message,
        method=decision.method,
        url=str(response.request.url),
        status_code=response.status_code,
        command_fingerprint=fingerprint,
        response_artifact=str(response_path),
        metadata_artifact=str(metadata_path),
        redirect_chain=redirect_chain,
        facts=decision.facts,
        flag_candidates=decision.flag_candidates,
        made_progress=True,
        redacted=body.redacted,
        sanitizer_findings=findings_to_dict(body.findings),
    )
    worker._emit(
        "worker.http_request",
        {
            "method": decision.method,
            "url": str(response.request.url),
            "status_code": response.status_code,
            "accepted": response.status_code < 500,
            "redirect_count": max(0, len(redirect_chain) - 1),
            "fingerprint": fingerprint,
        },
    )
    return report


def read_upload(worker: WorkerCore, relative_path: str) -> bytes:
    path_value = Path(relative_path)
    if path_value.is_absolute() or ".." in path_value.parts:
        raise WorkerExecutionError("multipart path must be a safe relative path")
    if path_value.parts and path_value.parts[0] == "challenge":
        if worker.workspace.challenge_files is None:
            raise WorkerExecutionError("challenge artifact root is unavailable")
        root = worker.workspace.challenge_files
        path = (root / Path(*path_value.parts[1:])).resolve()
    else:
        root = worker.workspace.root
        path = worker.workspace.resolve_relative(relative_path)
    if root.resolve() not in path.parents or path.is_symlink() or not path.is_file():
        raise WorkerExecutionError("multipart path is outside approved workspace roots")
    if path.stat().st_size > worker.budget.multipart_file_limit:
        raise WorkerExecutionError(
            f"multipart file exceeds {worker.budget.multipart_file_limit} bytes"
        )
    return path.read_bytes()
