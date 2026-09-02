"""Typed runtime capability manifest, provider, and immutable snapshot."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from ctf_agent.security import secure_write_json


class CapabilityCategory(StrEnum):
    TOOL = "tool"
    HTTP = "http"
    TCP = "tcp"
    AUTH = "auth"
    SKILL = "skill"
    BROWSER = "browser"


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    DISALLOWED = "disallowed"
    UNREACHABLE = "unreachable"
    UNAUTHENTICATED = "unauthenticated"
    MISCONFIGURED = "misconfigured"
    REFERENCE_ONLY = "reference_only"
    UNAVAILABLE = "unavailable"


class CapabilityDefinition(BaseModel):
    """One manifest-advertised runtime capability."""

    model_config = ConfigDict(frozen=True)

    name: str
    command: str | None
    category: CapabilityCategory = CapabilityCategory.TOOL
    required: bool = False
    allowed_by_default: bool = False
    requires_auth: bool = False
    reference_only: bool = False
    declared_status: CapabilityStatus | None = None
    installed: bool | None = None
    reachable: bool | None = None
    authenticated: bool | None = None
    version: str | None = None
    digest: str | None = None
    source: str | None = None
    reason: str | None = None
    version_args: tuple[str, ...] = ("--version",)


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    capabilities: tuple[CapabilityDefinition, ...]


class ToolProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    installed: bool
    reachable: bool
    authenticated: bool | None = None
    misconfigured: bool = False
    version: str | None = None
    digest: str | None = None
    source: str | None = None
    reason: str | None = None


class ContainerProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reachable: bool
    image_digest: str | None
    tools: tuple[ToolProbeResult, ...]
    reason: str | None = None


class CapabilityProbe(Protocol):
    def probe(
        self, image: str, definitions: tuple[CapabilityDefinition, ...]
    ) -> ContainerProbeResult: ...


class RuntimeCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    category: CapabilityCategory = CapabilityCategory.TOOL
    installed: bool | None
    allowed: bool
    reachable: bool | None
    authenticated: bool | None
    version: str | None
    digest: str | None = None
    status: CapabilityStatus
    source: str
    reason: str
    required: bool


class CapabilityPromptEntry(TypedDict):
    name: str
    category: str
    installed: bool | None
    allowed: bool
    reachable: bool | None
    authenticated: bool | None
    version: str | None
    digest: str | None
    status: str
    source: str
    reason: str
    required: bool


class CapabilityPromptContext(TypedDict):
    docker_image: str
    image_digest: str | None
    probe_reason: str | None
    digest: str
    capabilities: list[CapabilityPromptEntry]


class RuntimeCapabilitySnapshot(BaseModel):
    """Immutable truth source shared by runtime consumers and artifacts."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    docker_image: str
    image_digest: str | None
    capabilities: tuple[RuntimeCapability, ...]
    probe_reason: str | None = None
    digest: str = ""

    @model_validator(mode="after")
    def bind_digest(self) -> RuntimeCapabilitySnapshot:
        payload = {
            "schema_version": self.schema_version,
            "docker_image": self.docker_image,
            "image_digest": self.image_digest,
            "capabilities": [item.model_dump(mode="json") for item in self.capabilities],
            "probe_reason": self.probe_reason,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        actual = hashlib.sha256(encoded).hexdigest()
        if self.digest and self.digest != actual:
            raise CapabilityDigestMismatchError(self.digest, actual)
        object.__setattr__(self, "digest", actual)
        return self

    def require(self, name: str) -> RuntimeCapability:
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        raise UnknownCapabilityError(name=name)

    def prompt_context(self) -> CapabilityPromptContext:
        return {
            "docker_image": self.docker_image,
            "image_digest": self.image_digest,
            "probe_reason": self.probe_reason,
            "digest": self.digest,
            "capabilities": [
                {
                    "name": item.name,
                    "category": item.category.value,
                    "installed": item.installed,
                    "allowed": item.allowed,
                    "reachable": item.reachable,
                    "authenticated": item.authenticated,
                    "version": item.version,
                    "digest": item.digest,
                    "status": item.status.value,
                    "source": item.source,
                    "reason": item.reason,
                    "required": item.required,
                }
                for item in self.capabilities
            ],
        }

    def write(self, path: Path) -> None:
        secure_write_json(path, self.model_dump(mode="json"))


class CapabilityDigestMismatchError(ValueError):
    def __init__(self, supplied: str, actual: str) -> None:
        self.supplied = supplied
        self.actual = actual
        super().__init__(supplied, actual)

    def __str__(self) -> str:
        return f"capability snapshot digest mismatch: supplied {self.supplied}, got {self.actual}"


class UnknownCapabilityError(LookupError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)

    def __str__(self) -> str:
        return f"capability is not declared in the runtime manifest: {self.name}"


class StaticCapabilityProbe:
    """Deterministic probe adapter for tests and precomputed environments."""

    def __init__(self, result: ContainerProbeResult) -> None:
        self._result = result

    def probe(
        self, image: str, definitions: tuple[CapabilityDefinition, ...]
    ) -> ContainerProbeResult:
        del image, definitions
        return self._result


from ctf_agent.capability_provider import CapabilityProvider as CapabilityProvider  # noqa: E402


def default_capability_provider() -> CapabilityProvider:
    from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
    from ctf_agent.capability_probe import DockerCapabilityProbe, RuntimeCapabilityProbe

    return CapabilityProvider(
        DEFAULT_CAPABILITY_MANIFEST,
        RuntimeCapabilityProbe(DockerCapabilityProbe()),
    )
