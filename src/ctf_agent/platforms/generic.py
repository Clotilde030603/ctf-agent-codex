from __future__ import annotations

from pathlib import Path

from ctf_agent.ingestion.challenge_parser import parse_challenge_html
from ctf_agent.ingestion.downloader import download_attachments
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.base import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    SubmissionResult,
    challenge_from_mapping,
)
from ctf_agent.scope import HostScope


class GenericPlatformAdapter:
    platform = "generic"

    def __init__(self, scope: HostScope, session: ScopedAsyncSession | None = None) -> None:
        self.scope = scope
        self.session = session or ScopedAsyncSession(scope)

    async def authenticate(self) -> AuthSession:
        return AuthSession(authenticated=True, headers={"x-platform": self.platform})

    async def fetch_challenge(self, url: str) -> Challenge:
        self.scope.require(url, context="challenge URL")
        response = await self.session.get(url)
        response.raise_for_status()
        parsed = parse_challenge_html(response.text, str(response.url))
        parsed["url"] = str(response.url)
        return challenge_from_mapping(parsed, fallback_url=url)

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
        raise NotImplementedError(
            "generic adapter cannot submit flags without platform-specific endpoint"
        )

    async def resolve_submission(
        self, challenge: Challenge, flag: str
    ) -> SubmissionResult | None:
        return None

    async def capture_challenge(self, challenge: Challenge, destination: Path) -> Path | None:
        return None

    async def capture_verdict(self, challenge: Challenge, destination: Path) -> Path | None:
        return None
