from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.specialists.paths import UnsafeArtifactPathError, safe_existing_artifact_path

FLAG_RE = re.compile(r"[A-Za-z0-9_.-]+\{[^{}\r\n]{1,256}\}")
BASE64_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{8,}={0,2}(?![A-Za-z0-9+/_-])"
)
HEX_TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{8,}(?![0-9a-fA-F])")


@dataclass(frozen=True, slots=True)
class _CryptoHit:
    flag: str
    source_artifact: str
    source_location: str
    method: str
    token: str
    key: int | None = None
    key_bytes: bytes | None = None


class CryptoSpecialist:
    name = "crypto-deterministic"

    def supports(self, category: str) -> bool:
        lowered = category.lower()
        return any(token in lowered for token in ("crypto", "xor", "base64", "hex", "encoding"))

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        run_dir = Path(str(context["run_dir"]))
        triage = context.get("triage")
        triage_data = triage if isinstance(triage, dict) else {}
        hits, path_facts = _recover_crypto_hits(triage_data, run_dir)
        facts = [
            "checked triage strings for base64, hex, Caesar substitution, "
            "single-byte XOR, and known-prefix repeating XOR encodings",
            *_crypto_dependency_facts(),
            *path_facts,
        ]
        if not hits:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=facts,
                next_action=(
                    "try higher-cost crypto analysis; no deterministic low-risk "
                    "encoding yielded a flag"
                ),
                confidence=0.2,
            )

        _write_solver(run_dir, hits)
        emitted = _run_solver(run_dir)
        hits = [hit for hit in _dedupe_hits(hits) if hit.flag in emitted]
        if not hits:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=[
                    *facts,
                    "generated solver did not reproduce any deterministic crypto candidate",
                ],
                commands=["python3 solve.py"],
                reproduction_command="python3 solve.py",
                next_action="inspect source artifact drift or try higher-cost crypto analysis",
                confidence=0.2,
            )

        candidates = [
            FlagCandidate(
                value=hit.flag,
                source_artifact=hit.source_artifact,
                source_location=hit.source_location,
                derivation=_derivation(hit),
                solver_command="python3 solve.py",
                format_match=True,
                confidence=0.82,
            )
            for hit in hits
        ]
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="confirmed",
            facts=[
                *facts,
                f"recovered {len(candidates)} candidate(s) with deterministic decoding",
            ],
            artifacts=[candidate.source_artifact for candidate in candidates],
            commands=["python3 solve.py"],
            reproduction_command="python3 solve.py",
            flag_candidates=candidates,
            next_action="independent verification",
            confidence=max(candidate.confidence for candidate in candidates),
        )


def _recover_crypto_hits(
    triage_data: dict[str, object], run_dir: Path
) -> tuple[list[_CryptoHit], list[str]]:
    hits: list[_CryptoHit] = []
    facts: list[str] = []
    for scanned in _dict_items(triage_data.get("files")):
        raw_source = str(scanned.get("path") or scanned.get("relative_path") or "unknown")
        try:
            source = safe_existing_artifact_path(run_dir, raw_source)
        except UnsafeArtifactPathError as exc:
            facts.append(str(exc))
            continue
        for item in _dict_items(scanned.get("strings")):
            value = str(item.get("value") or "")
            offset = item.get("offset")
            location = f"offset {offset}" if isinstance(offset, int) else "string"
            hits.extend(_decode_tokens(value, source, location))
            hits.extend(_decode_caesar(value, source, location))
    return hits, list(dict.fromkeys(facts))


def _decode_tokens(text: str, source: str, location: str) -> list[_CryptoHit]:
    hits: list[_CryptoHit] = []
    for match in BASE64_TOKEN_RE.finditer(text):
        token = match.group(0).strip()
        token_location = f"{location}+{match.start()}"
        hits.extend(_decode_base64(token, source, token_location))
        hits.extend(_decode_xor(token, source, token_location))
    for match in HEX_TOKEN_RE.finditer(text):
        token = match.group(0).strip()
        token_location = f"{location}+{match.start()}"
        hits.extend(_decode_hex(token, source, token_location))
        hits.extend(_decode_xor(token, source, token_location))
    return hits


def _decode_base64(token: str, source: str, location: str) -> list[_CryptoHit]:
    normalized = token.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(padded.encode(), validate=True)
    except binascii.Error:
        return []
    return _flags_from_bytes(decoded, source, location, "base64", token)


def _decode_hex(token: str, source: str, location: str) -> list[_CryptoHit]:
    value = token[2:] if token.lower().startswith("0x") else token
    if len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return []
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return []
    return _flags_from_bytes(decoded, source, location, "hex", token)


def _decode_xor(token: str, source: str, location: str) -> list[_CryptoHit]:
    raw_candidates: list[bytes] = []
    hex_value = token[2:] if token.lower().startswith("0x") else token
    if len(hex_value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", hex_value):
        raw_candidates.append(bytes.fromhex(hex_value))
    try:
        raw_candidates.append(base64.b64decode(token + "=" * (-len(token) % 4), validate=True))
    except binascii.Error:
        pass

    hits: list[_CryptoHit] = []
    for raw in raw_candidates:
        if len(raw) < 4:
            continue
        for key in range(256):
            decoded = bytes(byte ^ key for byte in raw)
            hits.extend(_flags_from_bytes(decoded, source, location, "single-byte-xor", token, key))
        hits.extend(_decode_repeating_xor(raw, source, location, token))
    return hits


def _decode_repeating_xor(
    raw: bytes, source: str, location: str, token: str
) -> list[_CryptoHit]:
    hits: list[_CryptoHit] = []
    for prefix in (b"flag{", b"ctf{"):
        if len(raw) < len(prefix):
            continue
        for key_length in range(2, len(prefix) + 1):
            key: list[int | None] = [None] * key_length
            valid = True
            for index, plain in enumerate(prefix):
                derived = raw[index] ^ plain
                slot = index % key_length
                if key[slot] is not None and key[slot] != derived:
                    valid = False
                    break
                key[slot] = derived
            if not valid or any(value is None for value in key):
                continue
            key_bytes = bytes(value for value in key if value is not None)
            decoded = bytes(
                value ^ key_bytes[index % key_length]
                for index, value in enumerate(raw)
            )
            for hit in _flags_from_bytes(
                decoded, source, location, "known-prefix-repeating-xor", token
            ):
                hits.append(
                    _CryptoHit(
                        hit.flag,
                        hit.source_artifact,
                        hit.source_location,
                        hit.method,
                        hit.token,
                        key_bytes=key_bytes,
                    )
                )
    return hits


def _decode_caesar(text: str, source: str, location: str) -> list[_CryptoHit]:
    hits: list[_CryptoHit] = []
    for shift in range(1, 26):
        decoded = "".join(_shift_ascii_letter(char, -shift) for char in text)
        for match in FLAG_RE.finditer(decoded):
            if _is_low_risk_flag(match.group(0)):
                hits.append(
                    _CryptoHit(
                        match.group(0),
                        source,
                        location,
                        "caesar-substitution",
                        text,
                        key=shift,
                    )
                )
    return hits


def _shift_ascii_letter(char: str, shift: int) -> str:
    if "a" <= char <= "z":
        return chr((ord(char) - ord("a") + shift) % 26 + ord("a"))
    if "A" <= char <= "Z":
        return chr((ord(char) - ord("A") + shift) % 26 + ord("A"))
    return char


def _flags_from_bytes(
    data: bytes,
    source: str,
    location: str,
    method: str,
    token: str,
    key: int | None = None,
) -> list[_CryptoHit]:
    text = data.decode("utf-8", "ignore")
    return [
        _CryptoHit(match.group(0), source, location, method, token, key)
        for match in FLAG_RE.finditer(text)
        if _is_low_risk_flag(match.group(0))
    ]


def _dedupe_hits(hits: list[_CryptoHit]) -> list[_CryptoHit]:
    seen: set[str] = set()
    output: list[_CryptoHit] = []
    for hit in hits:
        key = f"{hit.flag}\0{hit.method}\0{hit.source_artifact}\0{hit.source_location}"
        if key in seen:
            continue
        seen.add(key)
        output.append(hit)
    return output


def _derivation(hit: _CryptoHit) -> list[str]:
    steps = ["triage string token", hit.method]
    if hit.key is not None:
        label = "Caesar shift" if hit.method == "caesar-substitution" else "xor key"
        steps.append(f"{label} 0x{hit.key:02x}")
    if hit.key_bytes is not None:
        steps.append(f"repeating xor key {hit.key_bytes.hex()}")
    return steps


def _crypto_dependency_facts() -> list[str]:
    facts: list[str] = []
    for module, label, install in (
        ("Crypto", "PyCryptodome", "pip install pycryptodome"),
        ("z3", "z3-solver", "pip install z3-solver"),
    ):
        if importlib.util.find_spec(module) is None:
            facts.append(
                f"missing dependency: {label}; install with `{install}`; "
                "deterministic stdlib fallback remains available"
            )
        else:
            facts.append(f"optional crypto dependency available: {label}")
    return facts


def _is_low_risk_flag(value: str) -> bool:
    return bool(re.fullmatch(r"(?i)(?:flag|ctf)\{[A-Za-z0-9_.:-]{1,128}\}", value))


def _write_solver(run_dir: Path, hits: list[_CryptoHit]) -> None:
    sources = sorted({hit.source_artifact for hit in hits})
    source = f'''#!/usr/bin/env python3
import base64
import binascii
from pathlib import Path
import re

FLAG_RE = re.compile({json.dumps(FLAG_RE.pattern)})
BASE64_TOKEN_RE = re.compile({json.dumps(BASE64_TOKEN_RE.pattern)})
HEX_TOKEN_RE = re.compile({json.dumps(HEX_TOKEN_RE.pattern)})
SOURCES = {json.dumps(sources, indent=2, sort_keys=True)}

def emit_flags(text, seen):
    for match in FLAG_RE.finditer(text):
        value = match.group(0)
        if not value.lower().startswith(("flag{{", "ctf{{")):
            continue
        if value not in seen:
            seen.add(value)
            print(value)

def decode_base64(token):
    normalized = token.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(padded.encode(), validate=True)
    except binascii.Error:
        return b""

def decode_hex(token):
    value = token[2:] if token.lower().startswith("0x") else token
    if len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return b""
    try:
        return bytes.fromhex(value)
    except ValueError:
        return b""

def xor_candidates(token):
    raw_values = []
    hex_value = token[2:] if token.lower().startswith("0x") else token
    if len(hex_value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", hex_value):
        raw_values.append(bytes.fromhex(hex_value))
    decoded = decode_base64(token)
    if decoded:
        raw_values.append(decoded)
    for raw in raw_values:
        if len(raw) < 4:
            continue
        for key in range(256):
            yield bytes(byte ^ key for byte in raw)

def repeating_xor_candidates(token):
    raw_values = []
    hex_value = token[2:] if token.lower().startswith("0x") else token
    if len(hex_value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", hex_value):
        raw_values.append(bytes.fromhex(hex_value))
    decoded = decode_base64(token)
    if decoded:
        raw_values.append(decoded)
    for raw in raw_values:
        for prefix in (b"flag{{", b"ctf{{"):
            if len(raw) < len(prefix):
                continue
            for key_length in range(2, len(prefix) + 1):
                key = [None] * key_length
                valid = True
                for index, plain in enumerate(prefix):
                    derived = raw[index] ^ plain
                    slot = index % key_length
                    if key[slot] is not None and key[slot] != derived:
                        valid = False
                        break
                    key[slot] = derived
                if valid and all(value is not None for value in key):
                    key_bytes = bytes(key)
                    yield bytes(
                        value ^ key_bytes[index % key_length]
                        for index, value in enumerate(raw)
                    )

def caesar_candidates(text):
    for shift in range(1, 26):
        decoded = []
        for char in text:
            if "a" <= char <= "z":
                char = chr((ord(char) - ord("a") - shift) % 26 + ord("a"))
            elif "A" <= char <= "Z":
                char = chr((ord(char) - ord("A") - shift) % 26 + ord("A"))
            decoded.append(char)
        yield "".join(decoded)

seen = set()
for relative in SOURCES:
    path = Path(relative)
    if not path.is_file():
        continue
    data = path.read_bytes()
    text = data.decode("utf-8", "ignore")
    for decoded in caesar_candidates(text):
        emit_flags(decoded, seen)
    for token in BASE64_TOKEN_RE.findall(text):
        emit_flags(decode_base64(token).decode("utf-8", "ignore"), seen)
        for decoded in xor_candidates(token):
            emit_flags(decoded.decode("utf-8", "ignore"), seen)
        for decoded in repeating_xor_candidates(token):
            emit_flags(decoded.decode("utf-8", "ignore"), seen)
    for token in HEX_TOKEN_RE.findall(text):
        emit_flags(decode_hex(token).decode("utf-8", "ignore"), seen)
        for decoded in xor_candidates(token):
            emit_flags(decoded.decode("utf-8", "ignore"), seen)
        for decoded in repeating_xor_candidates(token):
            emit_flags(decoded.decode("utf-8", "ignore"), seen)
'''
    solve_path = run_dir / "solve.py"
    solve_path.write_text(source, encoding="utf-8")
    solve_path.chmod(0o755)


def _run_solver(run_dir: Path, timeout_seconds: float = 5.0) -> set[str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "solve.py"],
            cwd=run_dir,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return set()
    if completed.returncode != 0:
        return set()
    return {match.group(0) for match in FLAG_RE.finditer(completed.stdout)}


def _dict_items(value: object) -> Iterable[dict[str, object]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, dict))
