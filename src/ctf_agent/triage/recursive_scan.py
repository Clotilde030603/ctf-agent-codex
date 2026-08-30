from __future__ import annotations

import hashlib
import mimetypes
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .extraction import (
    calculate_entropy,
    detect_source_language,
    extract_indicators,
    extract_strings,
)
from .tool_runner import run_tool
from .types import ExtractionRecord, ScannedFile, ToolRunResult, TriageReport, path_to_text

ARCHIVE_SUFFIXES = {".zip", ".jar", ".apk", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".tbz2", ".txz"}
DEFAULT_TOOLS = ("file", "strings", "exiftool", "binwalk", "checksec", "tshark")


@dataclass(slots=True)
class ScanConfig:
    max_depth: int = 3
    max_files: int = 1000
    max_file_size: int = 64 * 1024 * 1024
    max_total_extracted_size: int = 256 * 1024 * 1024
    strings_limit: int = 500
    tool_timeout_seconds: float = 5.0
    run_external_tools: bool = True
    external_tools: tuple[str, ...] = DEFAULT_TOOLS


def scan_path(
    path: Path | str, artifacts_dir: Path | str | None = None, config: ScanConfig | None = None
) -> TriageReport:
    config = config or ScanConfig()
    root = Path(path).resolve()
    if not root.exists():
        raise FileNotFoundError(root)

    if artifacts_dir is None:
        artifacts = Path(tempfile.mkdtemp(prefix="ctf-triage-artifacts-"))
    else:
        artifacts = Path(artifacts_dir).resolve()
        artifacts.mkdir(parents=True, exist_ok=True)

    report = TriageReport(root=path_to_text(root), artifacts_dir=path_to_text(artifacts))
    extracted_root = artifacts / "extracted"
    _scan_entry(
        root,
        root if root.is_dir() else root.parent,
        report,
        extracted_root,
        config,
        depth=0,
        parent_archive=None,
    )
    return report


def _scan_entry(
    path: Path,
    display_root: Path,
    report: TriageReport,
    extracted_root: Path,
    config: ScanConfig,
    *,
    depth: int,
    parent_archive: str | None,
) -> None:
    if len(report.files) >= config.max_files:
        report.warnings.append("max file count reached")
        return

    if path.is_dir():
        for child in sorted(path.iterdir(), key=lambda candidate: candidate.name):
            _scan_entry(
                child,
                display_root,
                report,
                extracted_root,
                config,
                depth=depth,
                parent_archive=parent_archive,
            )
        return
    if not path.is_file():
        return

    scanned = _scan_file(
        path,
        display_root,
        config,
        depth=depth,
        parent_archive=parent_archive,
        artifacts_dir=Path(report.artifacts_dir or "."),
    )
    report.files.append(scanned)

    if _looks_like_archive(path, scanned.magic) and depth < config.max_depth:
        destination = extracted_root / hashlib.sha256(path_to_text(path).encode()).hexdigest()[:16]
        try:
            records = _extract_archive(path, destination, depth + 1, config)
        except ValueError as exc:
            report.warnings.append(f"{path}: {exc}")
            return
        report.extractions.extend(records)
        for record in records:
            _scan_entry(
                Path(record.extracted_path),
                destination,
                report,
                extracted_root,
                config,
                depth=depth + 1,
                parent_archive=path_to_text(path),
            )


def _scan_file(
    path: Path,
    display_root: Path,
    config: ScanConfig,
    *,
    depth: int,
    parent_archive: str | None,
    artifacts_dir: Path,
) -> ScannedFile:
    stat = path.stat()
    data = (
        path.read_bytes()
        if stat.st_size <= config.max_file_size
        else path.read_bytes()[: config.max_file_size]
    )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    magic = _detect_magic(data)
    mime = _detect_mime(path, magic)
    tool_results = (
        _run_default_tools(path, artifacts_dir / "tools", config, magic)
        if config.run_external_tools
        else []
    )
    file_tool = _read_first_stdout(tool_results, "file")
    if file_tool:
        magic = file_tool.strip()

    strings = extract_strings(data, limit=config.strings_limit)
    indicators = extract_indicators(data, path)
    try:
        relative = path.relative_to(display_root)
    except ValueError:
        relative = Path(path.name)
    return ScannedFile(
        path=path_to_text(path),
        relative_path=path_to_text(relative),
        size=stat.st_size,
        sha256=digest.hexdigest(),
        mime=mime,
        magic=magic,
        entropy=round(calculate_entropy(data), 6),
        language=detect_source_language(path, data),
        parent_archive=parent_archive,
        extraction_depth=depth,
        strings=strings,
        indicators=indicators,
        tool_results=tool_results,
    )


def _detect_magic(data: bytes) -> str:
    if data.startswith(b"\x7fELF"):
        return "ELF executable"
    if data.startswith(b"MZ"):
        return "PE executable"
    if data.startswith(b"PK\x03\x04"):
        return "Zip archive"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if data.startswith(b"%PDF"):
        return "PDF document"
    if data.startswith((b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x0a\x0d\x0d\x0a")):
        return "PCAP packet capture"
    if data.startswith(b"\x1f\x8b"):
        return "gzip compressed data"
    if data[:265].endswith(b"ustar"):
        return "tar archive"
    if not data:
        return "empty"
    if all(byte in b"\t\r\n" or 32 <= byte <= 126 for byte in data[:4096]):
        return "ASCII text"
    return "data"


def _detect_mime(path: Path, magic: str) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    lowered = magic.lower()
    if "elf" in lowered:
        return "application/x-elf"
    if "pe executable" in lowered:
        return "application/vnd.microsoft.portable-executable"
    if "zip" in lowered:
        return "application/zip"
    if "pdf" in lowered:
        return "application/pdf"
    if "pcap" in lowered:
        return "application/vnd.tcpdump.pcap"
    if "png" in lowered:
        return "image/png"
    if "jpeg" in lowered:
        return "image/jpeg"
    if "text" in lowered:
        return "text/plain"
    return "application/octet-stream"


def _looks_like_archive(path: Path, magic: str) -> bool:
    lowered = magic.lower()
    return (
        path.suffix.lower() in ARCHIVE_SUFFIXES
        or "zip archive" in lowered
        or "tar archive" in lowered
    )


def _safe_target(base: Path, name: str) -> Path:
    if name.startswith("/") or name.startswith("\\"):
        raise ValueError(f"unsafe absolute archive member: {name}")
    target = (base / name).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise ValueError(f"archive path traversal blocked: {name}")
    return target


def _extract_archive(
    path: Path, destination: Path, depth: int, config: ScanConfig
) -> list[ExtractionRecord]:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(path):
        return _extract_zip(path, destination, depth, config)
    if tarfile.is_tarfile(path):
        return _extract_tar(path, destination, depth, config)
    return []


def _extract_zip(
    path: Path, destination: Path, depth: int, config: ScanConfig
) -> list[ExtractionRecord]:
    records: list[ExtractionRecord] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > config.max_total_extracted_size:
                raise ValueError("zip bomb guard exceeded total extracted size")
            target = _safe_target(destination, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            records.append(
                ExtractionRecord(
                    path_to_text(path), path_to_text(target), info.filename, depth, info.file_size
                )
            )
    return records


def _extract_tar(
    path: Path, destination: Path, depth: int, config: ScanConfig
) -> list[ExtractionRecord]:
    records: list[ExtractionRecord] = []
    total = 0
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            total += member.size
            if total > config.max_total_extracted_size:
                raise ValueError("archive bomb guard exceeded total extracted size")
            target = _safe_target(destination, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            records.append(
                ExtractionRecord(
                    path_to_text(path), path_to_text(target), member.name, depth, member.size
                )
            )
    return records


def _run_default_tools(
    path: Path, artifacts_dir: Path, config: ScanConfig, magic: str
) -> list[ToolRunResult]:
    results: list[ToolRunResult] = []
    tool_map = {
        "file": ["file", "--brief", path_to_text(path)],
        "strings": ["strings", "-a", "-n", "4", path_to_text(path)],
        "exiftool": ["exiftool", path_to_text(path)],
        "binwalk": ["binwalk", path_to_text(path)],
    }
    if "elf" in magic.lower():
        tool_map["checksec"] = ["checksec", "--file", path_to_text(path)]
    if "pcap" in magic.lower() or path.suffix.lower() in {".pcap", ".pcapng"}:
        tool_map["tshark"] = [
            "tshark",
            "-r",
            path_to_text(path),
            "-q",
            "-z",
            "io,phs",
        ]
    for tool in config.external_tools:
        command = tool_map.get(tool)
        if command is None:
            continue
        results.append(
            run_tool(command, artifacts_dir, timeout_seconds=config.tool_timeout_seconds, name=tool)
        )
    return results


def _read_first_stdout(results: list[ToolRunResult], tool: str) -> str | None:
    for result in results:
        if result.tool != tool or not result.stdout_artifact:
            continue
        path = Path(result.stdout_artifact)
        if path.exists():
            return path.read_text("utf-8", "replace")
    return None
