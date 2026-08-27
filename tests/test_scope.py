from __future__ import annotations

import pytest

from ctf_agent.scope import HostScope, ScopeViolation, require_same_origin


def test_scope_allows_same_host_and_relative_urls() -> None:
    scope = HostScope.from_url("https://ctf.example.com/challenges/1", allow_private_hosts=True)

    assert (
        scope.resolve_and_require("https://ctf.example.com/challenges/1", "/files/a.zip")
        == "https://ctf.example.com/files/a.zip"
    )


def test_scope_rejects_cross_host_attachment() -> None:
    scope = HostScope.from_url("https://ctf.example.com/challenges/1", allow_private_hosts=True)

    with pytest.raises(ScopeViolation):
        scope.resolve_and_require(
            "https://ctf.example.com/challenges/1", "https://evil.example.net/payload"
        )


def test_scope_rejects_url_credentials() -> None:
    with pytest.raises(ScopeViolation):
        require_same_origin("https://ctf.example.com", "https://user:pass@ctf.example.com/file")


def test_scope_rejects_private_by_default() -> None:
    scope = HostScope.from_url("https://ctf.example.com")

    with pytest.raises(ScopeViolation):
        scope.require("http://127.0.0.1:8080/admin")
