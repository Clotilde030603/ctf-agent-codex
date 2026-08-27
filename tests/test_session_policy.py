from __future__ import annotations

import asyncio

import httpx

from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.scope import HostScope


def test_get_retries_transient_response_with_bounded_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = ScopedAsyncSession(
        HostScope.from_url("https://ctf.test"),
        config=SessionConfig(retry_budget=1, rate_limit_per_second=1000),
        client=client,
    )

    response = asyncio.run(session.get("https://ctf.test/api/challenge"))
    asyncio.run(client.aclose())

    assert response.status_code == 200
    assert calls == 2


def test_post_is_never_automatically_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = ScopedAsyncSession(
        HostScope.from_url("https://ctf.test"),
        config=SessionConfig(retry_budget=5, rate_limit_per_second=1000),
        client=client,
    )

    response = asyncio.run(session.post("https://ctf.test/api/submit", json={"flag": "x"}))
    asyncio.run(client.aclose())

    assert response.status_code == 503
    assert calls == 1


def test_session_config_timeout_is_applied_to_owned_client() -> None:
    session = ScopedAsyncSession(
        HostScope.from_url("https://ctf.test"),
        config=SessionConfig(timeout_seconds=7, rate_limit_per_second=1000),
    )

    assert session.client.timeout.connect == 7
    asyncio.run(session.aclose())
