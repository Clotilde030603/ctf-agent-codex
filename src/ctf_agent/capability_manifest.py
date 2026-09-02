"""Built-in capability manifest for the versioned CTF runtime."""

from typing import Final

import httpx

from ctf_agent.capabilities import (
    CapabilityCategory,
    CapabilityDefinition,
    CapabilityManifest,
    CapabilityStatus,
)
from ctf_agent.skills import SkillRegistry

_TRUSTED_SKILL_DIGEST: Final = SkillRegistry.repository().digest

DEFAULT_CAPABILITY_MANIFEST: Final = CapabilityManifest(
    capabilities=(
        CapabilityDefinition(
            name="tcp-controller",
            command=None,
            category=CapabilityCategory.TCP,
            declared_status=CapabilityStatus.UNAVAILABLE,
            source="controller",
            reason="bounded controller TCP proxy is not implemented",
        ),
        CapabilityDefinition(
            name="auth:http-session",
            command=None,
            category=CapabilityCategory.AUTH,
            allowed_by_default=True,
            requires_auth=True,
            reachable=True,
            authenticated=False,
            source="auth-broker",
            reason="no authenticated controller session is registered",
        ),
        CapabilityDefinition(
            name="network:scoped-http",
            command=None,
            category=CapabilityCategory.HTTP,
            allowed_by_default=True,
            reachable=True,
            version=httpx.__version__,
            source="controller",
            reason="host-scoped HTTP session is enforced by the controller",
        ),
        CapabilityDefinition(
            name="browser:interaction",
            command=None,
            category=CapabilityCategory.BROWSER,
            allowed_by_default=True,
            installed=False,
            reachable=False,
            declared_status=CapabilityStatus.MISSING,
            source="local-runtime",
            reason="Playwright Chromium availability has not been observed",
        ),
        CapabilityDefinition(
            name="skill:trusted-runtime",
            command=None,
            category=CapabilityCategory.SKILL,
            allowed_by_default=True,
            installed=True,
            reachable=True,
            digest=_TRUSTED_SKILL_DIGEST,
            source="trusted-registry",
            reason="versioned trusted skill registry is available",
        ),
        *(
            CapabilityDefinition(
                name=name,
                command=name,
                required=required,
                allowed_by_default=allowed,
                version_args=version_args,
            )
            for name, required, allowed, version_args in (
                ("python", False, True, ("--version",)),
                ("python3", True, True, ("--version",)),
                ("file", True, True, ("--version",)),
                ("strings", True, True, ("--version",)),
                ("objdump", True, True, ("--version",)),
                ("readelf", True, True, ("--version",)),
                ("exiftool", True, True, ("-ver",)),
                ("binwalk", True, True, ("--help",)),
                ("checksec", True, True, ("--version",)),
                ("foremost", True, False, ("-V",)),
                ("tshark", True, False, ("--version",)),
                ("rizin", False, False, ("--version",)),
                ("ghidra", False, False, ("--version",)),
                ("angr", False, False, ("--version",)),
                ("gdb", False, False, ("--version",)),
                ("ROPgadget", False, False, ("--version",)),
                ("pwntools", False, False, ("--version",)),
            )
        ),
    )
)
