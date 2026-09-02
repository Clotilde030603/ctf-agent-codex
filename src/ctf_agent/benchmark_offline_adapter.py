"""Local platform boundary for scorer-owned offline benchmark workflows."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.benchmark_manifest import BenchmarkChallenge
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.schemas import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    SubmissionResult,
    SubmissionVerdict,
)


@dataclass(frozen=True, slots=True)
class OfflineBenchmarkAdapter(PlatformAdapter):
    """Expose one frozen local fixture through the normal platform boundary."""

    benchmark: BenchmarkChallenge
    source: Path

    async def authenticate(self) -> AuthSession:
        return AuthSession(authenticated=True)

    async def fetch_challenge(self, url: str) -> Challenge:
        return Challenge(
            id=self.benchmark.id,
            url=url,
            title=self.benchmark.id,
            description="Derive the flag from the attached frozen benchmark artifact.",
            category=self.benchmark.category,
            flag_policy=FlagPolicy.model_validate(self.benchmark.flag_policy),
            attachment_urls=[self.source.name],
        )

    async def download_attachments(
        self, challenge: Challenge, destination: Path
    ) -> list[Artifact]:
        del challenge
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / self.source.name
        shutil.copy2(self.source, target)
        return [
            Artifact(
                path=target,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                size=target.stat().st_size,
            )
        ]

    async def extract_flag_policy(self, challenge: Challenge) -> FlagPolicy:
        return challenge.flag_policy

    async def submit_flag(self, challenge: Challenge, flag: str) -> SubmissionResult:
        del challenge, flag
        return SubmissionResult(verdict=SubmissionVerdict.UNKNOWN)

    async def resolve_submission(
        self, challenge: Challenge, flag: str
    ) -> SubmissionResult | None:
        del challenge, flag
        return None

    async def capture_challenge(
        self, challenge: Challenge, destination: Path
    ) -> Path | None:
        del challenge, destination
        return None

    async def capture_verdict(
        self, challenge: Challenge, destination: Path
    ) -> Path | None:
        del challenge, destination
        return None
