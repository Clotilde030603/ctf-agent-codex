from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from ctf_agent.ingestion.downloader import download_attachments
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.base import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    SubmissionResult,
    challenge_from_mapping,
    parse_submission_verdict,
)
from ctf_agent.platforms.generic import GenericPlatformAdapter
from ctf_agent.scope import HostScope


class CTFdPlatformAdapter(GenericPlatformAdapter):
    platform = "ctfd"

    def __init__(self, base_url: str, session: ScopedAsyncSession | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        scope = session.scope if session else HostScope.from_url(base_url, allow_private_hosts=True)
        super().__init__(scope=scope, session=session or ScopedAsyncSession(scope))

    async def authenticate(self) -> AuthSession:
        response = await self.session.get(urljoin(self.base_url + "/", "api/v1/users/me"))
        if response.status_code == 200:
            return AuthSession(authenticated=True, headers={"x-platform": self.platform})
        return AuthSession(authenticated=False, headers={"x-platform": self.platform})

    async def fetch_challenge(self, url: str) -> Challenge:
        challenge_id = extract_ctfd_challenge_id(url)
        if challenge_id is None:
            return await super().fetch_challenge(url)
        api_url = urljoin(self.base_url + "/", f"api/v1/challenges/{challenge_id}")
        response = await self.session.get(api_url)
        if response.status_code == 404:
            return await super().fetch_challenge(url)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        challenge = challenge_from_mapping(data, fallback_url=url)
        challenge.url = url
        challenge.id = str(challenge_id)
        challenge.attachment_urls = [
            _absolute_file_url(self.base_url, item) for item in challenge.attachment_urls
        ]
        return challenge

    async def download_attachments(self, challenge: Challenge, destination: Path) -> list[Artifact]:
        return await download_attachments(
            self.session,
            challenge.attachment_urls,
            destination,
            base_url=challenge.url,
        )

    async def extract_flag_policy(self, challenge: Challenge) -> FlagPolicy:
        return challenge.flag_policy

    async def submit_flag(self, challenge: Challenge, flag: str) -> SubmissionResult:
        submit_url = urljoin(self.base_url + "/", "api/v1/challenges/attempt")
        response = await self.session.post(
            submit_url,
            json={"challenge_id": int(challenge.id), "submission": flag},
            headers={"Content-Type": "application/json"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        return parse_submission_verdict(payload, status_code=response.status_code)


def extract_ctfd_challenge_id(url: str) -> int | None:
    parsed = urlparse(url)
    match = re.search(r"/challenges/(?:[^/]+-)?(\d+)(?:$|[/?#])", parsed.path + "/")
    if not match:
        match = re.search(r"[?&](?:challenge|id)=(\d+)", parsed.query)
    return int(match.group(1)) if match else None


def _absolute_file_url(base_url: str, value: object) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("href") or value.get("name") or ""
    text = str(value)
    return urljoin(base_url + "/", text)
