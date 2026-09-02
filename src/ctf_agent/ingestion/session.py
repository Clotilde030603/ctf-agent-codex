from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ctf_agent.scope import HostScope


@dataclass(slots=True)
class SessionConfig:
    user_agent: str = "ctf-agent-codex/0.1"
    timeout_seconds: float = 30.0
    max_redirects: int = 5
    retry_budget: int = 2
    rate_limit_per_second: float = 2.0
    cookies_path: Path | None = None
    default_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retry_budget < 0:
            raise ValueError("retry_budget must be non-negative")
        if self.rate_limit_per_second <= 0:
            raise ValueError("rate_limit_per_second must be positive")


class ScopedAsyncSession:
    def __init__(
        self,
        scope: HostScope,
        *,
        config: SessionConfig | None = None,
        client: httpx.AsyncClient | None = None,
        request_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.scope = scope
        self.config = config or SessionConfig()
        self._owned_client = client is None
        self._request_observer = request_observer
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._authenticated_origin: str | None = None
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

    @property
    def authenticated(self) -> bool:
        return self._authenticated_origin is not None

    @property
    def authenticated_origin(self) -> str | None:
        return self._authenticated_origin

    def mark_authenticated(self, origin_url: str) -> None:
        self.scope.require(origin_url, context="authentication origin")
        self._authenticated_origin = _origin(origin_url)

    def clone(self, scope: HostScope) -> ScopedAsyncSession:
        clone = ScopedAsyncSession(
            scope,
            config=replace(self.config, cookies_path=None),
            client=self.client,
            request_observer=self._request_observer,
        )
        clone._authenticated_origin = self._authenticated_origin
        return clone

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.scope.require(url, context="request URL")
        redirects_remaining = self.config.max_redirects
        retries_remaining = (
            self.config.retry_budget if method.upper() in {"GET", "HEAD"} else 0
        )
        current_url = url
        redirect_chain: list[dict[str, Any]] = []
        while True:
            await self._throttle()
            request = self.client.build_request(method, current_url, **kwargs)
            if (
                self._authenticated_origin is not None
                and _origin(current_url) != self._authenticated_origin
            ):
                for header in ("authorization", "cookie", "proxy-authorization"):
                    request.headers.pop(header, None)
            response = await self.client.send(request, follow_redirects=False)
            if self._request_observer is not None:
                self._request_observer(
                    {
                        "method": method,
                        "url": str(response.request.url),
                        "status_code": response.status_code,
                        "redirect": response.status_code in {301, 302, 303, 307, 308},
                    }
                )
            redirect_chain.append(
                {
                    "method": method,
                    "url": str(response.request.url),
                    "status_code": response.status_code,
                }
            )
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                if retries_remaining > 0:
                    retries_remaining -= 1
                    await asyncio.sleep(
                        _retry_delay(
                            response,
                            self.config.retry_budget - retries_remaining,
                        )
                    )
                    continue
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.extensions["ctf_redirect_chain"] = redirect_chain
                return response
            location = response.headers.get("location")
            if not location:
                response.extensions["ctf_redirect_chain"] = redirect_chain
                return response
            if redirects_remaining <= 0:
                raise httpx.TooManyRedirects("redirect limit exceeded", request=response.request)
            current_url = urljoin(str(response.url), location)
            self.scope.require(current_url, context="redirect target")
            method = "GET" if response.status_code in {301, 302, 303} else method
            kwargs.pop("content", None)
            kwargs.pop("data", None)
            kwargs.pop("json", None)
            kwargs.pop("files", None)
            redirects_remaining -= 1

    async def _throttle(self) -> None:
        interval = 1.0 / self.config.rate_limit_per_second
        async with self._rate_lock:
            now = time.monotonic()
            remaining = interval - (now - self._last_request_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()

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

    def import_playwright_storage_state(self, path: Path) -> None:
        """Import cookies without persisting local/session storage in the HTTP layer."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        for cookie in payload.get("cookies", []):
            self.client.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.scheme.lower()}://{parsed.hostname or ''}:{port}"


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            parsed_retry_after = float(retry_after)
            return min(max(parsed_retry_after, 0.0), 5.0)
        except ValueError:
            pass
    return float(min(0.25 * (2 ** max(attempt - 1, 0)), 2.0))
