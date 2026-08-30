from __future__ import annotations

import asyncio

import pytest

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


@pytest.mark.parametrize("profile", [REV_PROFILE, PWN_PROFILE])
def test_toolchain_specialist_reports_dependencies_and_triage(
    profile: ToolchainProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    available = {"file", "strings", "objdump", "readelf", "checksec"}
    monkeypatch.setattr(
        "ctf_agent.specialists.toolchain.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in available else None,
    )
    monkeypatch.setattr(
        "ctf_agent.specialists.toolchain.importlib.util.find_spec", lambda _name: None
    )
    specialist = ToolchainSpecialist(profile)
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

    result = asyncio.run(specialist.solve(hypothesis(profile.category), context))

    assert result.status == "inconclusive"
    assert any("typed" in fact and "toolchain profile" in fact for fact in result.facts)
    assert any("native binary triage target" in fact for fact in result.facts)
    assert any("missing dependency" in fact for fact in result.facts)
    assert result.next_action
