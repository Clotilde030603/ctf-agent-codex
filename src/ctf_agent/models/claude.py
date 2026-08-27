from __future__ import annotations

from collections.abc import Iterable

from ctf_agent.models.base import ModelBackendError, ModelRequest, ModelResponse


class ClaudeStubBackend:
    """Deterministic, testable Claude-shaped backend used until real auth exists."""

    def __init__(self, responses: Iterable[str | ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request.validate()
        self.requests.append(request)
        if not self._responses:
            raise ModelBackendError("claude stub has no remaining responses")

        response = self._responses.pop(0)
        if isinstance(response, ModelResponse):
            return response
        return ModelResponse(content=response, raw={"content": response, "backend": "claude-stub"})
