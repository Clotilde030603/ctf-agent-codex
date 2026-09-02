"""Capability probing and runtime snapshot construction."""

from __future__ import annotations

from ctf_agent.capabilities import (
    CapabilityCategory,
    CapabilityDefinition,
    CapabilityManifest,
    CapabilityProbe,
    CapabilityStatus,
    RuntimeCapability,
    RuntimeCapabilitySnapshot,
    ToolProbeResult,
)


def _resolve_capability(
    definition: CapabilityDefinition,
    probed: ToolProbeResult | None,
    *,
    image: str,
    image_reachable: bool,
    allowed_tools: frozenset[str] | None,
    probe_reason: str | None,
) -> RuntimeCapability:
    allowed = (
        definition.allowed_by_default
        if allowed_tools is None or definition.category is not CapabilityCategory.TOOL
        else definition.name in allowed_tools
    )
    if definition.reference_only:
        status = CapabilityStatus.REFERENCE_ONLY
    elif definition.declared_status is CapabilityStatus.UNAVAILABLE:
        status = CapabilityStatus.UNAVAILABLE
        allowed = False
    else:
        installed = probed.installed if probed is not None else definition.installed
        reachable = (
            image_reachable and probed.reachable
            if probed is not None
            else definition.reachable
        )
        authenticated = (
            probed.authenticated if probed is not None else definition.authenticated
        )
        if definition.declared_status is not None:
            status = definition.declared_status
        elif definition.category is CapabilityCategory.TOOL and not image_reachable:
            status = CapabilityStatus.UNREACHABLE
        elif installed is False:
            status = CapabilityStatus.MISSING
        elif reachable is False:
            status = CapabilityStatus.UNREACHABLE
        elif probed is not None and probed.misconfigured:
            status = CapabilityStatus.MISCONFIGURED
        elif definition.requires_auth and authenticated is not True:
            status = CapabilityStatus.UNAUTHENTICATED
        elif not allowed:
            status = CapabilityStatus.DISALLOWED
        else:
            status = CapabilityStatus.AVAILABLE
    installed = probed.installed if probed is not None else definition.installed
    reachable = (
        image_reachable and probed.reachable
        if probed is not None
        else definition.reachable
    )
    authenticated = probed.authenticated if probed is not None else definition.authenticated
    reason = (
        "installed but command policy disallows execution"
        if status is CapabilityStatus.DISALLOWED
        else (probed.reason if probed is not None else None)
        or definition.reason
        or probe_reason
        or "available"
    )
    return RuntimeCapability(
        name=definition.name,
        category=definition.category,
        installed=installed,
        allowed=allowed,
        reachable=reachable,
        authenticated=authenticated,
        version=probed.version if probed is not None else definition.version,
        digest=probed.digest if probed is not None else definition.digest,
        status=status,
        source=(probed.source if probed is not None else None)
        or definition.source
        or f"container:{image}",
        reason=reason,
        required=definition.required,
    )


class CapabilityProvider:
    """Construct immutable runtime capability snapshots from probe results."""

    def __init__(self, manifest: CapabilityManifest, probe: CapabilityProbe) -> None:
        self._manifest = manifest
        self._probe = probe

    def snapshot(
        self,
        image: str,
        *,
        allowed_tools: frozenset[str] | None = None,
        authenticated_session: bool = False,
    ) -> RuntimeCapabilitySnapshot:
        probed = self._probe.probe(image, self._manifest.capabilities)
        by_name = {item.name: item for item in probed.tools}
        definitions = tuple(
            definition.model_copy(update={"authenticated": authenticated_session})
            if definition.name == "auth:http-session"
            else definition
            for definition in self._manifest.capabilities
        )
        capabilities = tuple(
            _resolve_capability(
                definition,
                by_name.get(definition.name),
                image=image,
                image_reachable=probed.reachable,
                allowed_tools=allowed_tools,
                probe_reason=probed.reason,
            )
            for definition in definitions
        )
        return RuntimeCapabilitySnapshot(
            docker_image=image,
            image_digest=probed.image_digest,
            capabilities=capabilities,
            probe_reason=probed.reason,
        )
