from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from ctf_agent.context_projector import (
    POLICY_VERSION,
    ContextProjector,
    ProjectedPrompt,
    ProjectedSection,
    render_codex_prompt,
    render_legacy_payload,
)
from ctf_agent.context_projector.events import projection_item_events
from ctf_agent.models.base import ModelBackendError, ModelRequest, ModelResponse
from ctf_agent.security import protect_directory, protect_file

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ProjectionEventObserver = Callable[[str, dict[str, Any]], None]

DEFAULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "content": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["content"],
}


class CodexCliBackend:
    """Model backend that invokes Codex CLI non-interactively.

    The model response channel is the file provided to --output-last-message.
    Stdout/stderr are treated only as bounded diagnostics.
    """

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        executable: str = "codex",
        model: str | None = None,
        reasoning_effort: str | None = None,
        cwd: Path | None = None,
        sandbox: SandboxMode = "workspace-write",
        timeout_seconds: float = 120.0,
        max_prompt_bytes: int = 1_000_000,
        max_output_bytes: int = 1_000_000,
        recent_report_limit: int = 3,
        projection_artifacts_dir: Path | None = None,
        projection_event_observer: ProjectionEventObserver | None = None,
    ) -> None:
        if command is not None and not command:
            raise ValueError("command must not be empty")
        if not executable.strip():
            raise ValueError("executable must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_prompt_bytes <= 0:
            raise ValueError("max_prompt_bytes must be positive")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        if recent_report_limit < 0:
            raise ValueError("recent_report_limit must not be negative")

        self._legacy_command = tuple(command) if command is not None else None
        self._executable = executable
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._cwd = cwd
        self._sandbox = sandbox
        self._timeout_seconds = timeout_seconds
        self._max_prompt_bytes = max_prompt_bytes
        self._max_output_bytes = max_output_bytes
        self._recent_report_limit = recent_report_limit
        self._projection_artifacts_dir = projection_artifacts_dir
        self._projection_event_observer = projection_event_observer

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request.validate()
        if self._legacy_command is not None:
            return await self._complete_legacy_command(request)

        projection = self._project(request, render_codex_prompt)
        prompt = projection.rendered.encode()

        with tempfile.TemporaryDirectory(prefix="ctf-agent-codex-") as directory:
            tempdir = Path(directory)
            schema_path = tempdir / "output-schema.json"
            final_path = tempdir / "final-message.json"
            schema = request.output_schema or DEFAULT_OUTPUT_SCHEMA
            schema_path.write_text(json.dumps(schema, sort_keys=True), encoding="utf-8")

            stdout, stderr = await self._run(self._command(schema_path, final_path), prompt)
            if len(stdout) > self._max_output_bytes or len(stderr) > self._max_output_bytes:
                raise ModelBackendError("codex diagnostic output exceeded byte limit")
            if not final_path.is_file():
                detail = _decode(stderr or stdout, limit=1_000)
                raise ModelBackendError(f"codex did not write final message file: {detail}")
            if final_path.stat().st_size > self._max_output_bytes:
                raise ModelBackendError(
                    f"codex final message exceeds {self._max_output_bytes} byte limit"
                )
            response = ModelResponse.from_final_message(final_path.read_text(encoding="utf-8"))
            return self._with_projection(response, projection.manifest.model_dump(mode="json"))

    async def _complete_legacy_command(self, request: ModelRequest) -> ModelResponse:
        projection = self._project(request, render_legacy_payload)
        payload = projection.rendered.encode()

        assert self._legacy_command is not None
        stdout, stderr = await self._run(list(self._legacy_command), payload, cwd=None)
        if len(stdout) > self._max_output_bytes or len(stderr) > self._max_output_bytes:
            raise ModelBackendError("codex diagnostic output exceeded byte limit")
        try:
            decoded = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise ModelBackendError("codex backend returned invalid JSON") from exc
        response = ModelResponse.from_json(decoded)
        return self._with_projection(response, projection.manifest.model_dump(mode="json"))

    def _project(
        self,
        request: ModelRequest,
        renderer: Callable[[ModelRequest, tuple[ProjectedSection, ...]], str],
    ) -> ProjectedPrompt:
        role = request.role.strip().lower() if request.role and request.role.strip() else "solver"
        started = {
            "role": role,
            "budget_bytes": self._max_prompt_bytes,
            "policy_version": POLICY_VERSION,
        }
        self._emit_projection_event("context.projection_started", started)
        try:
            projection = ContextProjector(
                self._max_prompt_bytes,
                recent_report_limit=self._recent_report_limit,
            ).project(request, renderer)
        except ModelBackendError as exc:
            failed = started | {
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            failure_identity = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in failed.items() if key != "message"},
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]
            self._write_projection_record(role, failure_identity, failed)
            self._emit_projection_event("context.projection_failed", failed)
            raise
        manifest = projection.manifest
        call_id = manifest.input_sha256[:16]
        payload = manifest.model_dump(mode="json")
        self._write_projection_record(role, call_id, payload)
        for item_event in projection_item_events(manifest):
            self._emit_projection_event("context.projection_item", item_event)
        self._emit_projection_event(
            "context.projection_completed",
            {
                "role": role,
                "call_id": call_id,
                "included": payload["included"],
                "summarized": payload["summarized"],
                "omitted": payload["omitted"],
                "original_bytes": payload["original_bytes"],
                "final_bytes": payload["final_bytes"],
                "input_sha256": payload["input_sha256"],
                "output_sha256": payload["output_sha256"],
                "policy_version": payload["policy_version"],
            },
        )
        return projection

    def _write_projection_record(self, role: str, call_id: str, payload: dict[str, Any]) -> None:
        if self._projection_artifacts_dir is None:
            return
        protect_directory(self._projection_artifacts_dir)
        safe_role = re.sub(r"[^a-z0-9_.-]+", "-", role).strip("-") or "solver"
        path = self._projection_artifacts_dir / f"{safe_role}-{call_id}-manifest.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        protect_file(path)

    def _emit_projection_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._projection_event_observer is not None:
            self._projection_event_observer(event_type, payload)

    def _command(self, schema_path: Path, final_path: Path) -> list[str]:
        command = [
            self._executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            self._sandbox,
            "--ignore-user-config",
            "--ignore-rules",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(final_path),
        ]
        if self._model:
            command.extend(("--model", self._model))
        if self._reasoning_effort:
            command.extend(("-c", f'model_reasoning_effort="{self._reasoning_effort}"'))
        if self._cwd:
            command.extend(("--cd", str(self._cwd)))
        command.append("-")
        return command

    async def _run(
        self,
        command: list[str],
        prompt: bytes,
        *,
        cwd: Path | None = None,
    ) -> tuple[bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd if cwd is None else cwd,
            )
        except FileNotFoundError as exc:
            raise ModelBackendError(f"codex executable not found: {self._executable}") from exc
        except OSError as exc:
            raise ModelBackendError(f"could not start codex executable: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ModelBackendError(
                f"codex backend timed out after {self._timeout_seconds:g} seconds"
            ) from exc

        if process.returncode != 0:
            detail = _decode(stderr or stdout, limit=2_000)
            raise ModelBackendError(f"codex backend exited with {process.returncode}: {detail}")
        return stdout, stderr

    @staticmethod
    def _render_prompt(request: ModelRequest) -> str:
        projection = ContextProjector(1_000_000_000).project(request, render_codex_prompt)
        return projection.rendered

    @staticmethod
    def _with_projection(response: ModelResponse, manifest: dict[str, Any]) -> ModelResponse:
        metadata = response.metadata | {"projection_manifest": manifest}
        return ModelResponse(content=response.content, raw=response.raw, metadata=metadata)


def _decode(value: bytes, *, limit: int) -> str:
    text = value[:limit].decode(errors="replace").strip()
    if len(value) > limit:
        return text + "..."
    return text
