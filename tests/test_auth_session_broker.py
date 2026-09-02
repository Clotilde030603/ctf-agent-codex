from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ctf_agent.auth_broker import AuthSessionStatus
from ctf_agent.events import EventLedger
from ctf_agent.ingestion.session import ScopedAsyncSession, SessionConfig
from ctf_agent.scope import HostScope
from ctf_agent.workers import WorkerDecision


def AuthSessionBroker():
    spec = importlib.util.find_spec("ctf_agent.auth_broker")
    assert spec is not None, "AuthSessionBroker module must exist"
    module = importlib.import_module("ctf_agent.auth_broker")
    broker_type = getattr(module, "AuthSessionBroker", None)
    assert callable(broker_type), "AuthSessionBroker must be callable"
    return broker_type()


def test_missing_process_local_session_is_explicitly_unavailable() -> None:
    broker = AuthSessionBroker()
    metadata = broker.metadata(None)
    assert metadata.status is AuthSessionStatus.UNAVAILABLE
    assert metadata.origin is None


@pytest.mark.asyncio
async def test_authenticated_same_origin_lane_request_uses_opaque_handle() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer auth-secret"},
        cookies={"session": "cookie-secret"},
    )
    session = ScopedAsyncSession(
        HostScope.from_url("https://challenge.test"),
        config=SessionConfig(rate_limit_per_second=1000),
        client=client,
    )
    session.mark_authenticated("https://challenge.test")
    broker = AuthSessionBroker()
    handle = broker.register(session)

    async with broker.clone_lane(handle, HostScope.from_url("https://challenge.test")) as lane:
        response = await lane.get("https://challenge.test/private")

    await client.aclose()
    assert response.status_code == 200
    assert requests[0].headers["authorization"] == "Bearer auth-secret"
    assert "session=cookie-secret" in requests[0].headers["cookie"]
    assert "auth-secret" not in repr(handle)
    assert "cookie-secret" not in repr(handle)
    assert not hasattr(handle, "cookies")
    assert not hasattr(handle, "headers")


@pytest.mark.asyncio
async def test_cross_origin_redirect_never_receives_credentials() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "challenge.test":
            return httpx.Response(
                302,
                headers={"location": "https://assets.test/result"},
                request=request,
            )
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer redirect-secret"},
        cookies={"session": "redirect-cookie"},
    )
    source_scope = HostScope.from_url(
        "https://challenge.test", extra_hosts=["https://assets.test"]
    )
    session = ScopedAsyncSession(
        source_scope,
        config=SessionConfig(rate_limit_per_second=1000),
        client=client,
    )
    session.mark_authenticated("https://challenge.test")
    broker = AuthSessionBroker()

    async with broker.clone_lane(broker.register(session), source_scope) as lane:
        response = await lane.get("https://challenge.test/start")

    await client.aclose()
    assert response.status_code == 200
    assert len(requests) == 2
    assert requests[0].headers.get("authorization") == "Bearer redirect-secret"
    assert "redirect-cookie" in requests[0].headers.get("cookie", "")
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers


def test_model_credentials_are_rejected_and_handle_events_contain_no_secret(
    tmp_path: Path,
) -> None:
    for header in ("Cookie", "Authorization"):
        with pytest.raises(ValidationError):
            WorkerDecision.model_validate(
                {
                    "action": "http_request",
                    "method": "GET",
                    "url": "https://challenge.test/private",
                    "headers": {header: "model-secret"},
                }
            )

    ledger = EventLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    ledger.append("run", "auth.ready", {"auth_handle": "auth_opaque"})
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"auth_handle": "auth_opaque"}), encoding="utf-8")

    persisted = (tmp_path / "events.jsonl").read_text() + checkpoint.read_text()
    assert "cookie-secret" not in persisted
    assert "auth-secret" not in persisted
