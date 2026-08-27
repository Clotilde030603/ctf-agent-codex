from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

from ctf_agent.models.base import ModelBackendError, ModelRequest, ModelResponse


class CodexCliBackend:
    """Model backend that invokes Codex CLI with JSON-over-stdin/stdout."""

    def __init__(
        self,
        command: Sequence[str] = ("codex", "exec", "--json"),
        timeout_seconds: float = 120.0,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request.validate()
        payload = json.dumps(
            {
                "prompt": request.prompt,
                "system": request.system,
                "context": request.context,
            },
            sort_keys=True,
        ).encode()

        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ModelBackendError("codex backend timed out") from exc

        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            raise ModelBackendError(f"codex backend exited with {process.returncode}: {message}")

        try:
            decoded = json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise ModelBackendError("codex backend returned invalid JSON") from exc

        return ModelResponse.from_json(decoded)
