from __future__ import annotations

import pytest
import typer

from ctf_agent.cli import _role_efforts, _validated_settings
from ctf_agent.config import Settings


def test_role_specific_effort_overrides_common_shorthand() -> None:
    defaults = Settings()

    efforts = _role_efforts("medium", "low", None, "high", defaults)

    assert efforts == {
        "planner_effort": "low",
        "solver_effort": "medium",
        "verifier_effort": "high",
    }


def test_invalid_reasoning_effort_fails_before_execution() -> None:
    with pytest.raises(typer.BadParameter):
        _validated_settings({**Settings().model_dump(), "solver_effort": "impossible"})
