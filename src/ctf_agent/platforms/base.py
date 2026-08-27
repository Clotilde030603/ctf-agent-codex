from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ctf_agent.schemas import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    SubmissionResult,
    SubmissionVerdict,
)

Verdict = SubmissionVerdict

__all__ = [
    "Artifact",
    "AuthSession",
    "Challenge",
    "FlagPolicy",
    "PlatformAdapter",
    "SubmissionResult",
    "SubmissionVerdict",
    "Verdict",
    "challenge_from_mapping",
    "merge_attachment_urls",
    "parse_submission_verdict",
]


class PlatformAdapter(Protocol):
    async def authenticate(self) -> AuthSession: ...

    async def fetch_challenge(self, url: str) -> Challenge: ...

    async def download_attachments(
        self,
        challenge: Challenge,
        destination: Path,
    ) -> list[Artifact]: ...

    async def extract_flag_policy(self, challenge: Challenge) -> FlagPolicy: ...

    async def submit_flag(self, challenge: Challenge, flag: str) -> SubmissionResult: ...

    async def capture_challenge(self, challenge: Challenge, destination: Path) -> Path | None: ...

    async def capture_verdict(self, challenge: Challenge, destination: Path) -> Path | None: ...


def challenge_from_mapping(data: Mapping[str, Any], *, fallback_url: str) -> Challenge:
    attachments = data.get("attachment_urls") or data.get("attachments") or data.get("files") or []
    if isinstance(attachments, str):
        attachments = [attachments]
    service_urls = (
        data.get("service_hosts") or data.get("service_urls") or data.get("services") or []
    )
    if isinstance(service_urls, str):
        service_urls = [service_urls]
    flag_policy = data.get("flag_policy")
    if not isinstance(flag_policy, FlagPolicy):
        flag_policy = FlagPolicy(
            pattern=str(data.get("flag_pattern") or FlagPolicy().pattern),
            prefix=data.get("flag_prefix"),
            examples=[str(data["flag_format"])] if data.get("flag_format") else [],
        )
    return Challenge(
        url=str(data.get("url") or fallback_url),
        id=str(
            data.get("id") or data.get("challenge_id") or fallback_url.rstrip("/").split("/")[-1]
        ),
        title=str(data.get("title") or data.get("name") or "Untitled challenge"),
        description=str(data.get("description") or ""),
        category=str(data.get("category") or "misc"),
        points=_int_or_none(data.get("points") or data.get("value")),
        flag_policy=flag_policy,
        attachment_urls=[str(item) for item in attachments],
        service_hosts=[str(item) for item in service_urls],
        metadata=dict(data.get("metadata") or {}),
    )


def merge_attachment_urls(challenge: Challenge, discovered: Sequence[str]) -> Challenge:
    seen = set(challenge.attachment_urls)
    merged = list(challenge.attachment_urls)
    for url in discovered:
        if url not in seen:
            seen.add(url)
            merged.append(url)
    challenge.attachment_urls = merged
    return challenge


def parse_submission_verdict(
    payload: str | Mapping[str, Any], *, status_code: int | None = None
) -> SubmissionResult:
    text = _payload_text(payload).lower()
    message = _payload_message(payload)
    if status_code == 429 or "rate limit" in text or "too many" in text:
        verdict = Verdict.RATE_LIMITED
    elif "already solved" in text:
        verdict = Verdict.ALREADY_SOLVED
    elif any(
        token in text for token in ("incorrect", "wrong", "invalid", "not correct", "try again")
    ):
        verdict = Verdict.WRONG
    elif any(token in text for token in ("correct", "accepted", "success", "solved", "congrat")):
        verdict = Verdict.ACCEPTED
    else:
        verdict = Verdict.UNKNOWN
    return SubmissionResult(verdict=verdict, message=message, status_code=status_code)


def _payload_text(payload: str | Mapping[str, Any]) -> str:
    if isinstance(payload, str):
        return payload
    values: list[str] = []
    for key in ("status", "message", "error", "data", "response"):
        value = payload.get(key)
        if value is not None:
            values.append(str(value))
    return " ".join(values) or str(payload)


def _payload_message(payload: str | Mapping[str, Any]) -> str:
    if isinstance(payload, str):
        return payload.strip()
    for key in ("message", "error", "data"):
        value = payload.get(key)
        if value is not None:
            return str(value).strip()
    return str(payload)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
