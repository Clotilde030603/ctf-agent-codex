from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse


class ScopeViolation(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HostScope:
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    allow_subdomains: bool = False
    allow_private_hosts: bool = False

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        extra_hosts: Iterable[str] = (),
        allow_subdomains: bool = False,
        allow_private_hosts: bool = False,
    ) -> HostScope:
        host = normalize_host(url)
        hosts = {host, *(normalize_host(item) for item in extra_hosts)}
        return cls(
            allowed_hosts=frozenset(hosts),
            allow_subdomains=allow_subdomains,
            allow_private_hosts=allow_private_hosts,
        )

    def allows(self, url: str) -> bool:
        host = normalize_host(url)
        if not self.allow_private_hosts and _is_private_host(host):
            return False
        if host in self.allowed_hosts:
            return True
        if self.allow_subdomains:
            return any(host.endswith(f".{allowed}") for allowed in self.allowed_hosts)
        return False

    def require(self, url: str, *, context: str = "URL") -> None:
        if not self.allows(url):
            raise ScopeViolation(f"{context} is outside allowed scope: {url}")

    def resolve_and_require(self, base_url: str, candidate: str, *, context: str = "URL") -> str:
        absolute = urljoin(base_url, candidate)
        self.require(absolute, context=context)
        return absolute


def normalize_host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.scheme not in {"http", "https"}:
        raise ScopeViolation(f"unsupported URL scheme: {parsed.scheme or '<missing>'}")
    if parsed.username or parsed.password:
        raise ScopeViolation("credentials in URLs are not allowed")
    host = parsed.hostname
    if not host:
        raise ScopeViolation(f"missing host in URL: {value}")
    return host.rstrip(".").lower().encode("idna").decode("ascii")


def require_same_origin(base_url: str, candidate: str, *, allow_subdomains: bool = False) -> str:
    scope = HostScope.from_url(
        base_url, allow_subdomains=allow_subdomains, allow_private_hosts=True
    )
    return scope.resolve_and_require(base_url, candidate)


def _is_private_host(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return host in {"localhost"} or host.endswith(".localhost")
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
