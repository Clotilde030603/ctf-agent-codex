from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ModelBackendError(RuntimeError):
    """Raised when a model backend cannot return a valid structured response."""


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    system: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ModelBackendError("model prompt must not be empty")


@dataclass(frozen=True)
class ModelResponse:
    content: str
    raw: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: Any) -> ModelResponse:
        if not isinstance(payload, dict):
            raise ModelBackendError("model response must be a JSON object")

        content = payload.get("content")
        if content is None:
            content = payload.get("result")
        if content is None:
            content = payload.get("answer")
        if content is None:
            content = payload.get("text")
        if not isinstance(content, str) or not content.strip():
            raise ModelBackendError("model response JSON must include non-empty content")

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ModelBackendError("model response metadata must be an object when present")

        return cls(content=content, raw=payload, metadata=metadata)


@runtime_checkable
class ModelBackend(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a structured model response for the supplied request."""
        ...
