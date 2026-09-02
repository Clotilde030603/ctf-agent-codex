from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ctf_agent.skills import SkillRuntime


class ModelBackendError(RuntimeError):
    """Raised when a model backend cannot return a valid structured response."""


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    system: str | None = None
    developer: str | None = None
    role: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    skill_runtime: SkillRuntime | None = None

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ModelBackendError("model prompt must not be empty")
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise ModelBackendError("model output schema must be a JSON object")


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

    @classmethod
    def from_final_message(cls, value: str) -> ModelResponse:
        content = value.strip()
        if not content:
            raise ModelBackendError("model final message must not be empty")

        try:
            payload = json_loads(content)
        except ValueError as exc:
            raise ModelBackendError("model final message must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise ModelBackendError("model final message must be a JSON object")

        if any(key in payload for key in ("content", "result", "answer", "text")):
            return cls.from_json(payload)
        return cls(content=content, raw=payload)


@runtime_checkable
class ModelBackend(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a structured model response for the supplied request."""
        ...


def json_loads(value: str) -> Any:
    import json

    return json.loads(value)
