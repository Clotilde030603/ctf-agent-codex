"""Deterministic redaction for logs and write-up source material."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from re import Pattern
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTION = "[REDACTED]"


@dataclass(frozen=True)
class SanitizationFinding:
    """A redaction event without retaining the original secret value."""

    kind: str
    count: int


@dataclass(frozen=True)
class SanitizationResult:
    """Sanitized text plus a compact finding summary."""

    text: str
    findings: tuple[SanitizationFinding, ...] = field(default_factory=tuple)

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: Pattern[str]
    replacement: str


class SecretSanitizer:
    """Redacts credentials while preserving CTF artifacts such as flags."""

    _SECRET_QUERY_KEYS = {
        "access_token",
        "api_key",
        "auth",
        "csrf",
        "key",
        "password",
        "secret",
        "session",
        "sig",
        "signature",
        "token",
    }

    def __init__(self, extra_patterns: Iterable[str | Pattern[str]] = ()) -> None:
        self._rules: tuple[_Rule, ...] = (
            _Rule(
                "authorization_header",
                re.compile(r"(?im)^(\s*authorization\s*:\s*)(?:bearer\s+)?[^\r\n]+"),
                rf"\1Bearer {REDACTION}",
            ),
            _Rule(
                "cookie_header",
                re.compile(r"(?im)^(\s*cookie\s*:\s*)[^\r\n]+"),
                rf"\1{REDACTION}",
            ),
            _Rule(
                "set_cookie_header",
                re.compile(r"(?im)^(\s*set-cookie\s*:\s*)[^\r\n]+"),
                rf"\1{REDACTION}",
            ),
            _Rule(
                "key_value_secret",
                re.compile(
                    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|csrf[_-]?token|session[_-]?id)"
                    r"(\s*[:=]\s*)([\"']?)[^\"'\s;&]+(\3)"
                ),
                rf"\1\2\3{REDACTION}\4",
            ),
            _Rule(
                "private_key",
                re.compile(
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                    re.DOTALL,
                ),
                REDACTION,
            ),
            _Rule(
                "high_entropy_token",
                re.compile(
                    r"\b(?=[A-Za-z0-9+/=_-]{32,}\b)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)"
                    r"[A-Za-z0-9+/=_-]{32,}\b"
                ),
                REDACTION,
            ),
        ) + tuple(
            _Rule(
                "custom_secret",
                re.compile(pattern) if isinstance(pattern, str) else pattern,
                REDACTION,
            )
            for pattern in extra_patterns
        )

    def sanitize(self, text: str | bytes | None) -> SanitizationResult:
        if text is None:
            return SanitizationResult("")
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")

        sanitized, url_count = self._redact_url_query_values(text)
        findings: list[SanitizationFinding] = []
        if url_count:
            findings.append(SanitizationFinding("url_query_secret", url_count))

        for rule in self._rules:
            sanitized, count = rule.pattern.subn(rule.replacement, sanitized)
            if count:
                findings.append(SanitizationFinding(rule.kind, count))
        return SanitizationResult(sanitized, tuple(findings))

    def assert_clean(self, text: str | bytes | None) -> None:
        result = self.sanitize(text)
        if result.redacted:
            kinds = ", ".join(f"{finding.kind}:{finding.count}" for finding in result.findings)
            raise ValueError(f"unsanitized secret material detected: {kinds}")

    def _redact_url_query_values(self, text: str) -> tuple[str, int]:
        url_pattern = re.compile(r"https?://[^\s<>'\")]+")
        redactions = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal redactions
            raw_url = match.group(0)
            split = urlsplit(raw_url)
            if not split.query:
                return raw_url
            changed = False
            pairs: list[tuple[str, str]] = []
            for key, value in parse_qsl(split.query, keep_blank_values=True):
                if key.lower() in self._SECRET_QUERY_KEYS and value != REDACTION:
                    pairs.append((key, REDACTION))
                    changed = True
                    redactions += 1
                else:
                    pairs.append((key, value))
            if not changed:
                return raw_url
            return urlunsplit(
                (split.scheme, split.netloc, split.path, urlencode(pairs), split.fragment)
            )

        return url_pattern.sub(replace, text), redactions
