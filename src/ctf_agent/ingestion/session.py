from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from ctf_agent.scope import HostScope


@dataclass(slots=True)
class SessionConfig:
    user_agent: str = "ctf-agent-codex/0.1"
    timeout_seconds: float = 30.0
    max_redirects: int = 5
    cookies_path: Path | None = None
    default_headers: dict[str, str] = field(default_factory=dict)


class ScopedAsyncSession:
    def __init__(
        self,
        scope: HostScope,
        *,
        config: SessionConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.scope = scope
        self.config = config or SessionConfig()
        self._owned_client = client is None
        headers = {"User-Agent": self.config.user_agent, **self.config.default_headers}
        self.client = client or httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            headers=headers,
            follow_redirects=False,
        )
        self._load_cookies()

    async def __aenter__(self) -> ScopedAsyncSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.save_cookies()
        if self._owned_client:
            await self.client.aclose()

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.scope.require(url, context="request URL")
        redirects_remaining = self.config.max_redirects
        current_url = url
        while True:
            response = await self.client.request(method, current_url, **kwargs)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            if redirects_remaining <= 0:
                raise httpx.TooManyRedirects("redirect limit exceeded", request=response.request)
            current_url = urljoin(str(response.url), location)
            self.scope.require(current_url, context="redirect target")
            method = "GET" if response.status_code in {301, 302, 303} else method
            kwargs.pop("content", None)
            kwargs.pop("data", None)
            kwargs.pop("json", None)
            redirects_remaining -= 1

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        response = await self.request(method, url, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise ValueError(f"expected JSON object from {url}")
        return data

    def save_cookies(self) -> None:
        if not self.config.cookies_path:
            return
        self.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self.client.cookies.jar
        ]
        self.config.cookies_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.config.cookies_path.chmod(0o600)

    def _load_cookies(self) -> None:
        if not self.config.cookies_path or not self.config.cookies_path.exists():
            return
        payload = json.loads(self.config.cookies_path.read_text(encoding="utf-8"))
        for cookie in payload:
            self.client.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
