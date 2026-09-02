"""Persistence-boundary redaction helpers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ctf_agent.evidence.sanitizer import REDACTION, SecretSanitizer

_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[-_])(?:token|key|secret|password|passwd|session|cookie|auth|authorization|csrf|sig|signature|credential|jwt|code)(?:$|[-_])",
    re.IGNORECASE,
)
_SANITIZER = SecretSanitizer(redact_high_entropy=False)


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
        return redact_url(_SANITIZER.sanitize(value).text)
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTION
                if _SENSITIVE_QUERY_KEY.search(str(key)) and isinstance(item, str)
                else redact_persisted_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_persisted_value(item) for item in value]
    return value


def protect_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def protect_file(path: Path) -> None:
    protect_directory(path.parent)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
    path.chmod(0o600)


def _write_private_text(path: Path, value: str) -> None:
    protect_directory(path.parent)
    if path.exists():
        protect_file(path)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    protect_file(path)


def secure_write_text(path: Path, value: str) -> None:
    """Sanitize and write one owner-only durable text artifact."""
    _write_private_text(path, _SANITIZER.sanitize(value).text)


def secure_write_json(path: Path, value: Any, *, indent: int | None = 2) -> None:
    """Redact structured values before JSON serialization and durable writing."""
    sanitized = redact_persisted_value(value)
    _write_private_text(
        path,
        json.dumps(sanitized, indent=indent, sort_keys=True, default=str) + "\n",
    )
