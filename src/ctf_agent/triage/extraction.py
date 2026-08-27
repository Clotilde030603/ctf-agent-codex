from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from .types import ExtractedString, Indicator, path_to_text

URL_RE = re.compile(rb"https?://[^\s'\"<>)]{3,2048}", re.IGNORECASE)
IP_RE = re.compile(
    rb"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"
)
FLAG_RE = re.compile(rb"[A-Za-z0-9_.-]{0,40}\{[^{}\r\n]{1,160}\}")
CRYPTO_CONSTANT_RE = re.compile(
    rb"\b(?:"
    rb"65537|0x10001|BEGIN RSA PRIVATE KEY|BEGIN PRIVATE KEY|"
    rb"-----BEGIN|AES|DES|RC4|RSA|ECC|secp256k1|ed25519|"
    rb"MD5|SHA1|SHA-1|SHA256|SHA-256|base64|xor|nonce|iv|salt"
    rb")\b",
    re.IGNORECASE,
)

SOURCE_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".php": "php",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c/c++",
    ".cc": "c++",
    ".cpp": "c++",
    ".java": "java",
    ".kt": "kotlin",
    ".sh": "shell",
    ".ps1": "powershell",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
}


def calculate_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    total = len(data)
    counts = Counter(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def extract_strings(data: bytes, min_length: int = 4, limit: int = 500) -> list[ExtractedString]:
    results: list[ExtractedString] = []
    start: int | None = None
    chars: list[int] = []
    for index, value in enumerate(data):
        if 32 <= value <= 126 or value in (9,):
            if start is None:
                start = index
            chars.append(value)
            continue
        if start is not None and len(chars) >= min_length:
            results.append(ExtractedString(bytes(chars).decode("utf-8", "replace"), start))
            if len(results) >= limit:
                return results
        start = None
        chars = []
    if start is not None and len(chars) >= min_length and len(results) < limit:
        results.append(ExtractedString(bytes(chars).decode("utf-8", "replace"), start))
    return results


def detect_source_language(path: Path, data: bytes) -> str | None:
    suffix_language = SOURCE_LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
    if suffix_language:
        return suffix_language
    sample = data[:4096].decode("utf-8", "ignore").lower()
    if sample.startswith("#!") and "python" in sample:
        return "python"
    if "<?php" in sample:
        return "php"
    if "#include" in sample or "int main(" in sample:
        return "c/c++"
    if "function " in sample and ("document." in sample or "console." in sample):
        return "javascript"
    return None


def _line_for_offset(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _context_for_offset(data: bytes, offset: int, length: int, radius: int = 48) -> str:
    start = max(0, offset - radius)
    end = min(len(data), offset + length + radius)
    return data[start:end].decode("utf-8", "replace").replace("\x00", "\\x00")


def _make_indicator(kind: str, match: re.Match[bytes], data: bytes, path: Path) -> Indicator:
    value = match.group(0).decode("utf-8", "replace")
    offset = match.start()
    return Indicator(
        kind=kind,
        value=value,
        artifact_path=path_to_text(path),
        offset=offset,
        line=_line_for_offset(data, offset),
        context=_context_for_offset(data, offset, len(match.group(0))),
    )


def extract_indicators(data: bytes, path: Path, limit_per_kind: int = 100) -> list[Indicator]:
    indicators: list[Indicator] = []
    patterns = (
        ("url", URL_RE),
        ("ip", IP_RE),
        ("flag-like", FLAG_RE),
        ("crypto-constant", CRYPTO_CONSTANT_RE),
    )
    for kind, pattern in patterns:
        count = 0
        for match in pattern.finditer(data):
            indicators.append(_make_indicator(kind, match, data, path))
            count += 1
            if count >= limit_per_kind:
                break
    indicators.sort(
        key=lambda item: (
            item.artifact_path,
            item.offset if item.offset is not None else -1,
            item.kind,
        )
    )
    return indicators
