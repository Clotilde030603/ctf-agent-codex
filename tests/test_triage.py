from __future__ import annotations

import zipfile
from pathlib import Path

from ctf_agent.triage import ScanConfig, classify_report, scan_path
from ctf_agent.triage.extraction import calculate_entropy, extract_indicators, extract_strings
from ctf_agent.triage.tool_runner import run_tool


def test_extracts_strings_and_location_aware_indicators(tmp_path: Path) -> None:
    sample = b"hello\nfetch http://127.0.0.1:8080 and flag CTF{real_flag}\nkey=65537\n"
    artifact = tmp_path / "sample.txt"
    artifact.write_bytes(sample)

    strings = extract_strings(sample)
    indicators = extract_indicators(sample, artifact)

    assert any(item.value.startswith("fetch http://") and item.offset == 6 for item in strings)
    flag = next(item for item in indicators if item.kind == "flag-like")
    assert flag.value == "CTF{real_flag}"
    assert flag.line == 2
    assert flag.offset == sample.index(b"CTF{real_flag}")
    assert "127.0.0.1" in flag.context
    assert any(item.kind == "url" and item.value == "http://127.0.0.1:8080" for item in indicators)
    assert any(item.kind == "crypto-constant" and item.value == "65537" for item in indicators)


def test_entropy_bounds() -> None:
    assert calculate_entropy(b"") == 0.0
    assert calculate_entropy(b"A" * 100) == 0.0
    assert calculate_entropy(bytes(range(256))) > 7.0


def test_recursive_scan_extracts_safe_zip_and_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "input"
    root.mkdir()
    safe_zip = root / "safe.zip"
    with zipfile.ZipFile(safe_zip, "w") as archive:
        archive.writestr("nested/readme.txt", "visit https://example.com CTF{inside_zip}")

    bad_zip = root / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("../escape.txt", "nope")

    report = scan_path(
        root,
        tmp_path / "artifacts",
        ScanConfig(run_external_tools=False, max_depth=2),
    )

    paths = {Path(scanned.path).name for scanned in report.files}
    assert "safe.zip" in paths
    assert "readme.txt" in paths
    assert "bad.zip" in paths
    assert any("path traversal blocked" in warning for warning in report.warnings)
    nested = next(
        scanned for scanned in report.files if scanned.relative_path == "nested/readme.txt"
    )
    assert nested.parent_archive is not None
    assert nested.extraction_depth == 1
    assert any(indicator.value == "CTF{inside_zip}" for indicator in nested.indicators)


def test_tool_runner_preserves_raw_output_and_missing_tools(tmp_path: Path) -> None:
    result = run_tool(
        ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        tmp_path,
        timeout_seconds=5,
    )
    assert result.exit_code == 0
    assert result.stdout_artifact is not None
    assert Path(result.stdout_artifact).read_text() == "out\n"
    assert result.stderr_artifact is not None
    assert Path(result.stderr_artifact).read_text() == "err\n"

    missing = run_tool(["definitely-not-a-real-ctf-tool"], tmp_path)
    assert missing.missing is True
    assert missing.error is not None


def test_deterministic_classification_reports_evidence_and_missing_capabilities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "challenge"
    root.mkdir()
    (root / "server.py").write_text(
        "from flask import Flask\n# csrf token\n@app.route('/login')\ndef login(): pass\n"
    )
    (root / "crypto.txt").write_text("n = p*q with e = 65537 and RSA modulus factoring\n")

    report = scan_path(
        root,
        tmp_path / "artifacts",
        ScanConfig(
            run_external_tools=True, external_tools=("definitely-not-a-real-ctf-tool",), max_depth=1
        ),
    )
    classification = classify_report(report)

    assert classification.primary_category in {"web", "mixed"}
    assert classification.confidence >= 0.55
    assert classification.evidence
    assert (
        "httpx" in classification.recommended_tools or "python" in classification.recommended_tools
    )
    assert not classification.missing_capabilities


def test_scan_file_metadata_hash_magic_mime_and_language(tmp_path: Path) -> None:
    source = tmp_path / "solver.py"
    source.write_text("#!/usr/bin/env python3\nprint('CTF{from_source}')\n")

    report = scan_path(source, tmp_path / "artifacts", ScanConfig(run_external_tools=False))

    assert len(report.files) == 1
    scanned = report.files[0]
    assert scanned.relative_path == "solver.py"
    assert scanned.size == source.stat().st_size
    assert len(scanned.sha256) == 64
    assert scanned.mime in {"text/x-python", "text/plain"}
    assert scanned.magic == "ASCII text"
    assert scanned.language == "python"
    assert any(indicator.value == "CTF{from_source}" for indicator in scanned.indicators)
