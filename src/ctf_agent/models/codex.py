from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from ctf_agent.models.base import ModelBackendError, ModelRequest, ModelResponse

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]

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

        self._legacy_command = tuple(command) if command is not None else None
        self._executable = executable
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._cwd = cwd
        self._sandbox = sandbox
        self._timeout_seconds = timeout_seconds
        self._max_prompt_bytes = max_prompt_bytes
        self._max_output_bytes = max_output_bytes

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request.validate()
        if self._legacy_command is not None:
            return await self._complete_legacy_command(request)

        prompt = self._render_prompt(request).encode()
        if len(prompt) > self._max_prompt_bytes:
            raise ModelBackendError(
                f"codex prompt exceeds {self._max_prompt_bytes} byte limit"
            )

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
            return ModelResponse.from_final_message(final_path.read_text(encoding="utf-8"))

    async def _complete_legacy_command(self, request: ModelRequest) -> ModelResponse:
        payload = json.dumps(
            {
                "prompt": request.prompt,
                "system": request.system,
                "role": request.role,
                "context": request.context,
                "output_schema": request.output_schema,
            },
            sort_keys=True,
        ).encode()
        if len(payload) > self._max_prompt_bytes:
            raise ModelBackendError(
                f"codex prompt exceeds {self._max_prompt_bytes} byte limit"
            )

        assert self._legacy_command is not None
        stdout, stderr = await self._run(list(self._legacy_command), payload, cwd=None)
        if len(stdout) > self._max_output_bytes or len(stderr) > self._max_output_bytes:
            raise ModelBackendError("codex diagnostic output exceeded byte limit")
        try:
            decoded = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise ModelBackendError("codex backend returned invalid JSON") from exc
        return ModelResponse.from_json(decoded)

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
            raise ModelBackendError(
                f"codex backend exited with {process.returncode}: {detail}"
            )
        return stdout, stderr

    @staticmethod
    def _render_prompt(request: ModelRequest) -> str:
        sections: list[str] = []
        if request.role:
            sections.append(f"Role:\n{request.role.strip()}")
        if request.system:
            sections.append(f"System instructions:\n{request.system.strip()}")
        if request.context:
            sections.append(
                "Context JSON:\n"
                + json.dumps(request.context, indent=2, sort_keys=True, default=str)
            )
        sections.append(f"Task:\n{request.prompt.strip()}")
        return "\n\n".join(sections) + "\n"


def _decode(value: bytes, *, limit: int) -> str:
    text = value[:limit].decode(errors="replace").strip()
    if len(value) > limit:
        return text + "..."
    return text
