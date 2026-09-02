from __future__ import annotations

import anyio
import pytest

from ctf_agent.capabilities import (
    CapabilityDefinition,
    CapabilityManifest,
    CapabilityProvider,
    ContainerProbeResult,
    StaticCapabilityProbe,
    ToolProbeResult,
)
from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.toolchain import (
    PWN_PROFILE,
    REV_PROFILE,
    ToolchainProfile,
    ToolchainSpecialist,
)


def hypothesis(claim: str) -> Hypothesis:
    return Hypothesis(
        id="toolchain",
        claim=claim,
        expected_signal="binary observations",
        cost="medium",
        confidence=0.4,
        kill_condition="missing required tools",
        success_condition="model lane receives typed observations",
    )


def _snapshot(profile: ToolchainProfile):
    available = {"file", "strings", "objdump", "readelf", "checksec"}
    manifest = CapabilityManifest(
        capabilities=tuple(
            CapabilityDefinition(
                name=item.command,
                command=item.command,
                required=item.required,
                allowed_by_default=item.command in available,
            )
            for item in profile.requirements
        )
    )
    return CapabilityProvider(
        manifest,
        StaticCapabilityProbe(
            ContainerProbeResult(
                reachable=True,
                image_digest="sha256:toolchain",
                tools=tuple(
                    ToolProbeResult(
                        name=item.command,
                        installed=item.command in available,
                        reachable=True,
                        authenticated=True,
                        version="test-1" if item.command in available else None,
                        reason=(
                            None
                            if item.command in available
                            else "command not found in image"
                        ),
                    )
                    for item in profile.requirements
                ),
            )
        ),
    ).snapshot("test-tools:1")


@pytest.mark.parametrize("profile", [REV_PROFILE, PWN_PROFILE])
def test_toolchain_specialist_reports_dependencies_and_triage(
    profile: ToolchainProfile,
) -> None:
    specialist = ToolchainSpecialist(profile, _snapshot(profile))
    context = {
        "triage": {
            "files": [
                {
                    "path": "files/challenge",
                    "magic": "ELF executable",
                    "tool_results": [
                        {
                            "tool": "checksec",
                            "missing": False,
                            "exit_code": 0,
                            "stdout_artifact": "artifacts/checksec.stdout",
                        }
                    ],
                }
            ]
        }
    }

    result = anyio.run(specialist.solve, hypothesis(profile.category), context)

    assert result.status == "inconclusive"
    assert any("typed" in fact and "toolchain profile" in fact for fact in result.facts)
    assert any("native binary triage target" in fact for fact in result.facts)
    assert any("dependency unavailable" in fact for fact in result.facts)
    assert result.next_action
