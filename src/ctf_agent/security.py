"""Persistence-boundary redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[-_])(?:token|key|secret|password|passwd|session|auth|csrf|sig|signature|credential|jwt|code)(?:$|[-_])",
    re.IGNORECASE,
)


def redact_url(value: str) -> str:
    if not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlsplit(value)
    query = [
        (key, "REDACTED" if _SENSITIVE_QUERY_KEY.search(key) else item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    fragment = "REDACTED" if _SENSITIVE_QUERY_KEY.search(parsed.fragment) else parsed.fragment
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), fragment))


def redact_persisted_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_url(value)
    if isinstance(value, Mapping):
        return {str(key): redact_persisted_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_persisted_value(item) for item in value]
    return value
