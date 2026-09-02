"""Extracted workflow behavior."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from ctf_agent.security import secure_write_json

type JsonValue = (
    str | int | float | bool | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)


def _write_json(path: Path, value: JsonValue) -> None:
    secure_write_json(path, value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
