from __future__ import annotations

import httpx
import pytest

from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.detect import create_detected_adapter, detect_platform
from ctf_agent.platforms.generic import GenericPlatformAdapter
from ctf_agent.platforms.rctf import RCTFPlatformAdapter
from ctf_agent.scope import HostScope, ScopeViolation


@pytest.mark.asyncio
async def test_detects_ctfd_from_challenge_api() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/challenges/7":
            return httpx.Response(200, json={"success": True, "data": {"id": 7, "name": "warmup"}})
        return httpx.Response(404, json={})

    session = ScopedAsyncSession(
        HostScope.from_url("https://ctf.test", allow_private_hosts=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    detection = await detect_platform("https://ctf.test/challenges/7", session=session)
    await session.aclose()

    assert detection.kind == "ctfd"


@pytest.mark.asyncio
async def test_detects_rctf_from_good_challenges_kind() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me":
            return httpx.Response(404, json={})
        if request.url.path == "/api/v2/challs":
            return httpx.Response(200, json={"kind": "goodChallengesV2", "data": []})
        return httpx.Response(404, json={})

    session = ScopedAsyncSession(
        HostScope.from_url("https://rctf.test", allow_private_hosts=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    adapter = await create_detected_adapter("https://rctf.test/challs/web-warmup", session=session)
    await session.aclose()

    assert isinstance(adapter, RCTFPlatformAdapter)


@pytest.mark.asyncio
async def test_detection_falls_back_to_generic() -> None:
    session = ScopedAsyncSession(
        HostScope.from_url("https://plain.test", allow_private_hosts=True),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(404))
        ),
    )

    adapter = await create_detected_adapter("https://plain.test/problems/one", session=session)
    await session.aclose()

    assert isinstance(adapter, GenericPlatformAdapter)


@pytest.mark.asyncio
async def test_detection_redirects_remain_scoped() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/api/v1/challs"})

    session = ScopedAsyncSession(
        HostScope.from_url("https://rctf.test", allow_private_hosts=True),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    with pytest.raises(ScopeViolation):
        await detect_platform("https://rctf.test/challs/web-warmup", session=session)
    await session.aclose()
