from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.ctfd import CTFdPlatformAdapter, extract_ctfd_challenge_id
from ctf_agent.platforms.generic import GenericPlatformAdapter
from ctf_agent.platforms.rctf import RCTFPlatformAdapter, is_rctf_challenge_list
from ctf_agent.scope import HostScope

PlatformKind = Literal["ctfd", "rctf", "generic"]


@dataclass(frozen=True, slots=True)
class PlatformDetection:
    kind: PlatformKind
    confidence: float
    signal: str


async def detect_platform(
    challenge_url: str,
    *,
    session: ScopedAsyncSession | None = None,
    allow_private_hosts: bool = False,
) -> PlatformDetection:
    parsed = urlsplit(challenge_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    scoped_session = session or ScopedAsyncSession(
        HostScope.from_url(base_url, allow_private_hosts=allow_private_hosts)
    )
    close_session = session is None
    try:
        ctfd = await _probe_ctfd(scoped_session, base_url, challenge_url)
        if ctfd is not None:
            return ctfd
        rctf = await _probe_rctf(scoped_session, base_url)
        if rctf is not None:
            return rctf
        return PlatformDetection("generic", 0.2, "no known platform API signature")
    finally:
        if close_session:
            await scoped_session.aclose()


async def create_detected_adapter(
    challenge_url: str,
    *,
    session: ScopedAsyncSession | None = None,
    allow_private_hosts: bool = False,
    browser_storage_state: Path | None = None,
) -> GenericPlatformAdapter:
    parsed = urlsplit(challenge_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    scoped_session = session or ScopedAsyncSession(
        HostScope.from_url(base_url, allow_private_hosts=allow_private_hosts)
    )
    detection = await detect_platform(
        challenge_url, session=scoped_session, allow_private_hosts=allow_private_hosts
    )
    if detection.kind == "ctfd":
        return CTFdPlatformAdapter(
            base_url,
            session=scoped_session,
            browser_storage_state=browser_storage_state,
        )
    if detection.kind == "rctf":
        return RCTFPlatformAdapter(base_url, session=scoped_session)
    return GenericPlatformAdapter(scoped_session.scope, session=scoped_session)


async def _probe_ctfd(
    session: ScopedAsyncSession, base_url: str, challenge_url: str
) -> PlatformDetection | None:
    challenge_id = extract_ctfd_challenge_id(challenge_url)
    probes = []
    if challenge_id is not None:
        probes.append(f"{base_url}/api/v1/challenges/{challenge_id}")
    probes.append(f"{base_url}/api/v1/users/me")
    for url in probes:
        response = await session.get(url)
        if response.status_code >= 500:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and (
            "success" in payload or (isinstance(data, dict) and "name" in data)
        ):
            return PlatformDetection("ctfd", 0.9, f"CTFd API signature at {url}")
    return None


async def _probe_rctf(session: ScopedAsyncSession, base_url: str) -> PlatformDetection | None:
    for version in ("v2", "v1"):
        url = f"{base_url}/api/{version}/challs"
        response = await session.get(url)
        if response.status_code >= 500:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        if is_rctf_challenge_list(payload):
            return PlatformDetection("rctf", 0.95, f"rCTF challenge-list kind at {url}")
    return None
