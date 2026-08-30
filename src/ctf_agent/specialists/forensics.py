from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult
from ctf_agent.specialists.paths import UnsafeArtifactPathError, safe_existing_artifact_path

FLAG_RE = re.compile(r"[A-Za-z0-9_.-]+\{[^{}\r\n]{1,256}\}")
TEXTUAL_CHUNKS = {"tEXt", "zTXt", "iTXt"}


@dataclass(frozen=True, slots=True)
class _ForensicsHit:
    flag: str
    source_artifact: str
    source_location: str
    derivation: tuple[str, ...]


class ForensicsSpecialist:
    name = "forensics-deterministic"

    def supports(self, category: str) -> bool:
        lowered = category.lower()
        return any(
            token in lowered
            for token in ("forensics", "png", "metadata", "archive", "image", "stego")
        )

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        run_dir = Path(str(context["run_dir"]))
        triage = context.get("triage")
        triage_data = triage if isinstance(triage, dict) else {}
        hits, facts = _recover_forensics_hits(triage_data, run_dir)
        if not hits:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=[
                    *facts,
                    "no deterministic PNG text, metadata, nested extraction, "
                    "or string flag candidate found",
                ],
                next_action="run higher-cost forensics tooling if available; do not claim success",
                confidence=0.2,
            )

        hits = _dedupe_hits(hits)
        _write_solver(run_dir, hits)
        emitted = _run_solver(run_dir)
        hits = [hit for hit in hits if hit.flag in emitted]
        if not hits:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                facts=[
                    *facts,
                    "generated solver did not reproduce any deterministic forensics candidate",
                ],
                commands=["python3 solve.py"],
                reproduction_command="python3 solve.py",
                next_action="inspect source artifact drift or run higher-cost forensics tooling",
                confidence=0.2,
            )
        candidates = [
            FlagCandidate(
                value=hit.flag,
                source_artifact=hit.source_artifact,
                source_location=hit.source_location,
                derivation=list(hit.derivation),
                solver_command="python3 solve.py",
                format_match=True,
                confidence=0.8,
            )
            for hit in hits
        ]
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="confirmed",
            facts=[
                *facts,
                f"derived {len(candidates)} candidate(s) from deterministic forensics data",
            ],
            artifacts=[candidate.source_artifact for candidate in candidates],
            commands=["python3 solve.py"],
            reproduction_command="python3 solve.py",
            flag_candidates=candidates,
            next_action="independent verification",
            confidence=max(candidate.confidence for candidate in candidates),
        )


def _recover_forensics_hits(
    triage_data: dict[str, object], run_dir: Path
) -> tuple[list[_ForensicsHit], list[str]]:
    hits: list[_ForensicsHit] = []
    facts: list[str] = []
    for scanned in _dict_items(triage_data.get("files")):
        raw_source = str(scanned.get("path") or scanned.get("relative_path") or "unknown")
        try:
            source = safe_existing_artifact_path(run_dir, raw_source)
        except UnsafeArtifactPathError as exc:
            facts.append(str(exc))
            continue
        magic = str(scanned.get("magic") or "")
        if "pcap" in magic.lower() or source.lower().endswith((".pcap", ".pcapng")):
            facts.append(f"packet capture artifact detected: {source}")
            for tool_result in _dict_items(scanned.get("tool_results")):
                if tool_result.get("tool") == "tshark" and not tool_result.get("missing"):
                    facts.append(
                        f"tshark protocol hierarchy output linked for packet capture: {source}"
                    )
        if str(scanned.get("parent_archive") or ""):
            facts.append(f"nested extracted artifact present: {source}")
        hits.extend(_hits_from_indicators(scanned, source))
        hits.extend(_hits_from_strings(scanned, source))
        hits.extend(_hits_from_tool_outputs(scanned, run_dir))
        if "png" in magic.lower() or source.lower().endswith(".png"):
            png_hits, png_facts = _hits_from_png_text(run_dir, source)
            hits.extend(png_hits)
            facts.extend(png_facts)
    if not any("optional" in fact for fact in facts):
        facts.extend(_optional_tool_facts(triage_data))
    return hits, list(dict.fromkeys(facts))


def _hits_from_indicators(scanned: dict[str, object], source: str) -> list[_ForensicsHit]:
    hits: list[_ForensicsHit] = []
    for indicator in _dict_items(scanned.get("indicators")):
        if indicator.get("kind") != "flag-like":
            continue
        value = str(indicator.get("value") or "")
        if not value:
            continue
        location = (
            f"offset {indicator.get('offset')}"
            if indicator.get("offset") is not None
            else f"line {indicator.get('line', 'unknown')}"
        )
        hits.append(
            _ForensicsHit(
                value,
                source,
                location,
                ("triage recursive scan", "flag-like indicator with provenance"),
            )
        )
    return hits


def _hits_from_strings(scanned: dict[str, object], source: str) -> list[_ForensicsHit]:
    hits: list[_ForensicsHit] = []
    for item in _dict_items(scanned.get("strings")):
        value = str(item.get("value") or "")
        offset = item.get("offset")
        location = f"offset {offset}" if isinstance(offset, int) else "string"
        for match in FLAG_RE.finditer(value):
            hits.append(
                _ForensicsHit(
                    match.group(0),
                    source,
                    location,
                    ("triage strings", "forensics string artifact"),
                )
            )
    return hits


def _hits_from_tool_outputs(scanned: dict[str, object], run_dir: Path) -> list[_ForensicsHit]:
    hits: list[_ForensicsHit] = []
    for result in _dict_items(scanned.get("tool_results")):
        artifact = result.get("stdout_artifact")
        if not isinstance(artifact, str):
            continue
        try:
            source = safe_existing_artifact_path(run_dir, artifact)
        except UnsafeArtifactPathError:
            continue
        text = (run_dir / source).read_text("utf-8", "ignore")
        for match in FLAG_RE.finditer(text):
            hits.append(
                _ForensicsHit(
                    match.group(0),
                    source,
                    f"tool stdout from {result.get('tool', 'unknown')}",
                    ("external tool output", str(result.get("tool", "unknown"))),
                )
            )
    return hits


def _hits_from_png_text(run_dir: Path, source: str) -> tuple[list[_ForensicsHit], list[str]]:
    path = run_dir / source
    if not path.is_file():
        return [], [f"PNG artifact unavailable for direct chunk parse: {source}"]
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return [], []
    hits: list[_ForensicsHit] = []
    facts: list[str] = []
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8].decode("ascii", "replace")
        chunk_data = data[offset + 8 : offset + 8 + length]
        if offset + 12 + length > len(data):
            facts.append(f"truncated PNG chunk at offset {offset}")
            break
        if chunk_type in TEXTUAL_CHUNKS:
            text = _decode_png_text_chunk(chunk_type, chunk_data)
            facts.append(f"parsed PNG textual chunk {chunk_type} at offset {offset}")
            for match in FLAG_RE.finditer(text):
                hits.append(
                    _ForensicsHit(
                        match.group(0),
                        source,
                        f"PNG {chunk_type} chunk offset {offset}",
                        ("png textual chunk", chunk_type),
                    )
                )
        offset += 12 + length
    return hits, facts


def _decode_png_text_chunk(chunk_type: str, data: bytes) -> str:
    if chunk_type == "tEXt":
        return data.decode("latin-1", "replace")
    if chunk_type == "zTXt":
        parts = data.split(b"\x00", 2)
        if len(parts) < 3:
            return data.decode("latin-1", "replace")
        try:
            return (
                parts[0].decode("latin-1", "replace")
                + "\x00"
                + zlib.decompress(parts[2]).decode("latin-1", "replace")
            )
        except zlib.error:
            return data.decode("latin-1", "replace")
    if chunk_type == "iTXt":
        return data.decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


def _optional_tool_facts(triage_data: dict[str, object]) -> list[str]:
    facts: list[str] = []
    for scanned in _dict_items(triage_data.get("files")):
        for result in _dict_items(scanned.get("tool_results")):
            if result.get("missing") is True:
                tool = str(result.get("tool") or "unknown")
                install = {
                    "tshark": "install Wireshark/tshark or use the versioned CTF tool image",
                    "foremost": "install foremost or use the versioned CTF tool image",
                    "binwalk": "install binwalk or use the versioned CTF tool image",
                    "exiftool": "install ExifTool or use the versioned CTF tool image",
                }.get(tool, "install the tool in the CTF worker image")
                facts.append(
                    f"missing dependency: {tool}; {install}; "
                    "stdlib/string-analysis fallback remains available"
                )
    return list(dict.fromkeys(facts))


def _dedupe_hits(hits: list[_ForensicsHit]) -> list[_ForensicsHit]:
    seen: set[str] = set()
    output: list[_ForensicsHit] = []
    for hit in hits:
        key = f"{hit.flag}\0{hit.source_artifact}\0{hit.source_location}"
        if key in seen:
            continue
        seen.add(key)
        output.append(hit)
    return output


def _write_solver(run_dir: Path, hits: list[_ForensicsHit]) -> None:
    sources = sorted({hit.source_artifact for hit in hits})
    source = f'''#!/usr/bin/env python3
from pathlib import Path
import re
import struct
import zlib

FLAG_RE = re.compile({json.dumps(FLAG_RE.pattern)})
SOURCES = {json.dumps(sources, indent=2, sort_keys=True)}
TEXTUAL_CHUNKS = {json.dumps(sorted(TEXTUAL_CHUNKS))}

def emit_flags(text, seen):
    for match in FLAG_RE.finditer(text):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            print(value)

def decode_png_text_chunk(chunk_type, data):
    if chunk_type == "tEXt":
        return data.decode("latin-1", "replace")
    if chunk_type == "zTXt":
        parts = data.split(b"\\x00", 2)
        if len(parts) < 3:
            return data.decode("latin-1", "replace")
        try:
            return (
                parts[0].decode("latin-1", "replace")
                + "\\x00"
                + zlib.decompress(parts[2]).decode("latin-1", "replace")
            )
        except zlib.error:
            return data.decode("latin-1", "replace")
    if chunk_type == "iTXt":
        return data.decode("utf-8", "replace")
    return data.decode("utf-8", "replace")

def emit_png_text(data, seen):
    if not data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        return
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8].decode("ascii", "replace")
        chunk_data = data[offset + 8:offset + 8 + length]
        if offset + 12 + length > len(data):
            return
        if chunk_type in TEXTUAL_CHUNKS:
            emit_flags(decode_png_text_chunk(chunk_type, chunk_data), seen)
        offset += 12 + length

seen = set()
for relative in SOURCES:
    path = Path(relative)
    if not path.is_file():
        continue
    data = path.read_bytes()
    emit_flags(data.decode("latin-1", "ignore"), seen)
    emit_png_text(data, seen)
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
