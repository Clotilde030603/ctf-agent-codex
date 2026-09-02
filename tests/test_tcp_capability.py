from __future__ import annotations

from pathlib import Path

from ctf_agent.capabilities import CapabilityStatus
from ctf_agent.capability_manifest import DEFAULT_CAPABILITY_MANIFEST
from ctf_agent.models.base import ModelResponse
from ctf_agent.workers import LaneWorkspace, WorkerBudget, WorkerCore


class TcpBackend:
    async def complete(self, _request: object) -> ModelResponse:
        return ModelResponse(
            content=(
                '{"action":"tcp_connect","host":"unauthorized.test",'
                '"port":31337,"tcp_payload":"credential=secret"}'
            )
        )


def test_tcp_controller_capability_is_explicitly_unavailable() -> None:
    definition = next(
        item for item in DEFAULT_CAPABILITY_MANIFEST.capabilities if item.name == "tcp-controller"
    )

    assert definition.command is None
    assert definition.declared_status is CapabilityStatus.UNAVAILABLE


async def test_unavailable_tcp_action_never_connects_or_persists_payload(tmp_path: Path) -> None:
    worker = WorkerCore(
        TcpBackend(),
        LaneWorkspace(tmp_path / "lane"),
        budget=WorkerBudget(max_steps=1, max_no_progress_steps=1),
    )

    result = await worker.run("connect to the challenge service")

    assert result.reports[0].status == "failed"
    assert result.reports[0].message == "tcp-controller capability is unavailable"
    assert "unauthorized.test" not in result.model_dump_json()
    assert "credential=secret" not in result.model_dump_json()
    assert not list((tmp_path / "lane" / "artifacts").iterdir())
