from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ctf_agent.ingestion.downloader import download_attachments
from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.base import (
    Artifact,
    AuthSession,
    Challenge,
    FlagPolicy,
    SubmissionResult,
    SubmissionVerdict,
)
from ctf_agent.platforms.generic import GenericPlatformAdapter
from ctf_agent.scope import HostScope


class RCTFPlatformAdapter(GenericPlatformAdapter):
    platform = "rctf"

    def __init__(
        self,
        base_url: str,
        session: ScopedAsyncSession | None = None,
        *,
        allow_private_hosts: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        scope = session.scope if session else HostScope.from_url(
            base_url, allow_private_hosts=allow_private_hosts
        )
        super().__init__(scope=scope, session=session or ScopedAsyncSession(scope))

    async def authenticate(self) -> AuthSession:
        test_response = await self.session.get(urljoin(self.base_url + "/", "api/v1/auth/test"))
        auth_payload = _json_or_none(test_response)
        if _is_auth_test_success(auth_payload):
            return AuthSession(authenticated=True, headers={"x-platform": self.platform})
        return AuthSession(
            authenticated=False,
            headers={"x-platform": self.platform},
        )

    async def fetch_challenge(self, url: str) -> Challenge:
        challenge_id = extract_rctf_challenge_id(url)
        payload = await self._challenge_list()
        data = _challenge_items(payload)
        selected = _select_challenge(data, challenge_id)
        if selected is None:
            raise LookupError(f"rCTF challenge not found for URL: {url}")
        challenge = rctf_challenge_from_mapping(selected, fallback_url=url, base_url=self.base_url)
        self._expand_declared_scope(challenge)
        return challenge

    async def download_attachments(self, challenge: Challenge, destination: Path) -> list[Artifact]:
        return await download_attachments(
            self.session,
            challenge.attachment_urls,
            destination,
            base_url=challenge.url,
        )

    async def extract_flag_policy(self, challenge: Challenge) -> FlagPolicy:
        if challenge.metadata.get("hasFlag") is False:
            return FlagPolicy(pattern=r"$^")
        return challenge.flag_policy

    async def submit_flag(self, challenge: Challenge, flag: str) -> SubmissionResult:
        response = await self.session.post(
            urljoin(self.base_url + "/", f"api/v1/challs/{challenge.id}/submit"),
            json={"flag": flag},
            headers={"Content-Type": "application/json"},
        )
        return parse_rctf_submission(_json_or_text(response), status_code=response.status_code)

    async def resolve_submission(
        self, challenge: Challenge, flag: str
    ) -> SubmissionResult | None:
        payload = await self._challenge_list()
        selected = _select_challenge(_challenge_items(payload), challenge.id)
        if selected is None:
            return None
        if selected.get("solved") is True or selected.get("solvesByUser") is True:
            return SubmissionResult(
                verdict=SubmissionVerdict.ALREADY_SOLVED,
                message="rCTF challenge list reports solved state",
                status_code=200,
            )
        return None

    async def _challenge_list(self) -> Mapping[str, Any]:
        last_payload: Mapping[str, Any] | None = None
        for version in ("v2", "v1"):
            response = await self.session.get(urljoin(self.base_url + "/", f"api/{version}/challs"))
            if response.status_code == 404:
                continue
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, Mapping):
                last_payload = payload
                if is_rctf_challenge_list(payload):
                    return payload
        if last_payload is not None:
            return last_payload
        raise LookupError("rCTF challenge list endpoint unavailable")

    def _expand_declared_scope(self, challenge: Challenge) -> None:
        declared = [*challenge.attachment_urls, *challenge.service_hosts]
        self.scope = HostScope.from_url(
            challenge.url,
            extra_hosts=declared,
            allow_subdomains=self.scope.allow_subdomains,
            allow_private_hosts=self.scope.allow_private_hosts,
        )
        self.session.scope = self.scope


def is_rctf_challenge_list(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    kind = payload.get("kind")
    return kind in {"goodChallenges", "goodChallengesV2"} and isinstance(payload.get("data"), list)


def extract_rctf_challenge_id(url: str) -> str:
    parsed = urlparse(url)
    patterns = (
        r"/(?:challs|challenges|challenge)/([^/?#]+)",
        r"/([^/?#]+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path.rstrip("/"))
        if match:
            return match.group(1)
    query_match = re.search(r"(?:^|&)(?:id|challenge)=([^&]+)", parsed.query)
    if query_match:
        return query_match.group(1)
    return parsed.path.rstrip("/").split("/")[-1]


def rctf_challenge_from_mapping(
    data: Mapping[str, Any], *, fallback_url: str, base_url: str
) -> Challenge:
    challenge_id = str(data.get("id") or data.get("_id") or data.get("slug") or "")
    if not challenge_id:
        challenge_id = extract_rctf_challenge_id(fallback_url)
    attachments = [_absolute_rctf_file_url(base_url, challenge_id, item) for item in _files(data)]
    points = data.get("points") or data.get("score") or data.get("value")
    return Challenge(
        id=challenge_id,
        url=fallback_url,
        title=str(data.get("name") or data.get("title") or challenge_id),
        description=str(data.get("description") or ""),
        category=str(data.get("category") or data.get("categoryName") or "misc"),
        points=_int_or_none(points),
        flag_policy=FlagPolicy(),
        attachment_urls=attachments,
        metadata={
            "hasFlag": data.get("hasFlag"),
            "solved": data.get("solved"),
            "solvesByUser": data.get("solvesByUser"),
            "raw_kind": "rctf",
        },
    )


def parse_rctf_submission(
    payload: str | Mapping[str, Any], *, status_code: int | None = None
) -> SubmissionResult:
    kind = payload.get("kind") if isinstance(payload, Mapping) else None
    message = _rctf_message(payload)
    if kind == "goodFlag":
        verdict = SubmissionVerdict.ACCEPTED
    elif kind == "badFlag":
        verdict = SubmissionVerdict.WRONG
    elif kind == "badAlreadySolvedChallenge":
        verdict = SubmissionVerdict.ALREADY_SOLVED
    elif kind == "badRateLimit" or status_code == 429:
        verdict = SubmissionVerdict.RATE_LIMITED
    elif kind in {"badAuth", "badToken"} or status_code == 401:
        verdict = SubmissionVerdict.AUTH_REQUIRED
    else:
        verdict = SubmissionVerdict.UNKNOWN
    return SubmissionResult(verdict=verdict, message=message, status_code=status_code)


def _challenge_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    return []


def _select_challenge(
    items: list[Mapping[str, Any]], challenge_id: str | None
) -> Mapping[str, Any] | None:
    if not items:
        return None
    if not challenge_id:
        return items[0]
    normalized = _normalize_selector(challenge_id)
    for item in items:
        selectors = {
            str(item.get("id") or ""),
            str(item.get("_id") or ""),
            str(item.get("slug") or ""),
            _normalize_selector(str(item.get("name") or "")),
        }
        if normalized in selectors or challenge_id in selectors:
            return item
    return None


def _files(data: Mapping[str, Any]) -> list[object]:
    files = data.get("files") or data.get("attachments") or []
    if isinstance(files, str):
        return [files]
    if isinstance(files, list):
        return files
    return []


def _absolute_rctf_file_url(base_url: str, challenge_id: str, value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("href") or value.get("name") or value.get("id") or ""
    text = str(value)
    if text.startswith(("http://", "https://", "/")):
        return urljoin(base_url + "/", text)
    return urljoin(base_url + "/", f"api/v1/challs/{challenge_id}/files/{text}")


def _is_auth_test_success(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    kind = str(payload.get("kind") or "")
    return kind.startswith("good") or payload.get("data") is True


def _json_or_none(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _json_or_text(response: httpx.Response) -> str | Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    return payload if isinstance(payload, Mapping) else str(payload)


def _rctf_message(payload: str | Mapping[str, Any]) -> str:
    if isinstance(payload, str):
        return payload
    data = payload.get("data")
    if isinstance(data, Mapping) and data.get("timeLeft") is not None:
        return f"rate limited; timeLeft={data['timeLeft']}ms"
    if payload.get("message") is not None:
        return str(payload["message"])
    return str(payload.get("kind") or payload)


def _normalize_selector(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _int_or_none(value: object) -> int | None:
    if not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
