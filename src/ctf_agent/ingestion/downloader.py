from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ctf_agent.ingestion.session import ScopedAsyncSession
from ctf_agent.platforms.base import Artifact

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class DownloadSafetyError(ValueError):
    pass


async def download_attachments(
    session: ScopedAsyncSession,
    urls: list[str],
    destination: Path,
    *,
    base_url: str,
    max_bytes: int = 200 * 1024 * 1024,
) -> list[Artifact]:
    destination.mkdir(parents=True, exist_ok=True)
    artifacts: list[Artifact] = []
    for url in urls:
        absolute_url = session.scope.resolve_and_require(base_url, url, context="attachment URL")
        artifacts.append(
            await download_attachment(
                session,
                absolute_url,
                destination,
                max_bytes=max_bytes,
            )
        )
    return artifacts


async def download_attachment(
    session: ScopedAsyncSession,
    url: str,
    destination: Path,
    *,
    max_bytes: int = 200 * 1024 * 1024,
) -> Artifact:
    session.scope.require(url, context="attachment URL")
    response = await session.get(url)
    response.raise_for_status()
    filename = filename_from_response(url, response.headers)
    target = safe_destination(destination, filename)
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as handle:
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                target.unlink(missing_ok=True)
                raise DownloadSafetyError(f"attachment exceeds byte limit: {url}")
            digest.update(chunk)
            handle.write(chunk)
    return Artifact(
        path=target,
        sha256=digest.hexdigest(),
        size=size,
        source_url=url,
        media_type=response.headers.get("content-type"),
    )


def filename_from_response(url: str, headers: httpx.Headers | dict[str, str]) -> str:
    disposition = headers.get("content-disposition")
    if disposition:
        match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", disposition, re.I)
        if match:
            return sanitize_filename(unquote(match.group(1) or match.group(2)))
    path_name = Path(unquote(urlparse(url).path)).name
    return sanitize_filename(path_name or "attachment.bin")


def sanitize_filename(name: str) -> str:
    basename = name.replace("\\", "/").split("/")[-1]
    if basename in {".", ".."}:
        raise DownloadSafetyError(f"unsafe attachment filename: {name}")
    cleaned = _SAFE_NAME.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "attachment.bin"
    return cleaned[:160]


def safe_destination(destination: Path, filename: str) -> Path:
    root = destination.resolve()
    target = (root / sanitize_filename(filename)).resolve()
    if root != target.parent and root not in target.parents:
        raise DownloadSafetyError(f"attachment escapes destination: {filename}")
    return _dedupe(target)


def _dedupe(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise DownloadSafetyError(f"too many duplicate attachment names: {target.name}")
