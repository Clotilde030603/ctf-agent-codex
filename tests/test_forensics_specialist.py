from __future__ import annotations

import asyncio
import struct
import subprocess
import zlib
from pathlib import Path

from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.forensics import ForensicsSpecialist


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="H1",
        claim="forensics image metadata",
        expected_signal="metadata flag",
        cost="low",
        confidence=0.7,
        kill_condition="no deterministic artifact",
        success_condition="candidate",
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _write_png(path: Path, text: str) -> None:
    data = b"\x89PNG\r\n\x1a\n"
    data += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data += _png_chunk(b"tEXt", b"Comment\x00" + text.encode("latin-1"))
    data += _png_chunk(b"IEND", b"")
    path.write_bytes(data)


def test_forensics_supports_routes() -> None:
    specialist = ForensicsSpecialist()
    assert specialist.supports("forensics")
    assert specialist.supports("PNG metadata")
    assert not specialist.supports("crypto xor")


def test_forensics_recovers_png_text_chunk_and_writes_solver(tmp_path: Path) -> None:
    png = tmp_path / "files" / "image.png"
    png.parent.mkdir()
    _write_png(png, "hidden flag{png_text}")
    triage = {
        "files": [
            {
                "path": str(png),
                "magic": "PNG image",
                "strings": [],
                "indicators": [],
                "tool_results": [],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "confirmed"
    assert result.flag_candidates[0].value == "flag{png_text}"
    assert "png textual chunk" in result.flag_candidates[0].derivation
    completed = subprocess.run(
        ["python3", "solve.py"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.strip() == "flag{png_text}"
    solve_source = (tmp_path / "solve.py").read_text(encoding="utf-8")
    assert "flag{png_text}" not in solve_source

    _write_png(png, "hidden but no flag")
    altered = subprocess.run(
        ["python3", "solve.py"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "flag{png_text}" not in altered.stdout


def test_forensics_uses_nested_strings_and_tool_output(tmp_path: Path) -> None:
    stdout = tmp_path / "artifacts" / "exiftool.stdout"
    stdout.parent.mkdir()
    stdout.write_text("Comment: flag{tool_output}\n", encoding="utf-8")
    nested = tmp_path / "artifacts" / "nested.txt"
    nested.write_text("payload flag{nested_string}\n", encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(nested),
                "parent_archive": str(tmp_path / "files" / "archive.zip"),
                "strings": [{"value": "payload flag{nested_string}", "offset": 9}],
                "indicators": [],
                "tool_results": [{"tool": "exiftool", "stdout_artifact": str(stdout)}],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "confirmed"
    assert {candidate.value for candidate in result.flag_candidates} == {
        "flag{nested_string}",
        "flag{tool_output}",
    }
    assert any("nested extracted artifact present" in fact for fact in result.facts)
    solve_source = (tmp_path / "solve.py").read_text(encoding="utf-8")
    assert "flag{nested_string}" not in solve_source
    assert "flag{tool_output}" not in solve_source


def test_forensics_missing_optional_tools_is_fact_not_success(tmp_path: Path) -> None:
    triage = {
        "files": [
            {
                "path": "image.png",
                "magic": "PNG image",
                "strings": [],
                "indicators": [],
                "tool_results": [{"tool": "exiftool", "missing": True}],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert any("missing dependency: exiftool" in fact for fact in result.facts)
    assert result.next_action
    assert not (tmp_path / "solve.py").exists()


def test_forensics_links_pcap_and_tshark_observation(tmp_path: Path) -> None:
    pcap = tmp_path / "files" / "traffic.pcap"
    pcap.parent.mkdir()
    pcap.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 32)
    stdout = tmp_path / "artifacts" / "tshark.stdout"
    stdout.parent.mkdir()
    stdout.write_text("eth -> ip -> tcp\n", encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(pcap),
                "magic": "PCAP packet capture",
                "strings": [],
                "indicators": [],
                "tool_results": [
                    {
                        "tool": "tshark",
                        "missing": False,
                        "exit_code": 0,
                        "stdout_artifact": str(stdout),
                    }
                ],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(
            _hypothesis(), {"run_dir": str(tmp_path), "triage": triage}
        )
    )

    assert result.status == "inconclusive"
    assert any("packet capture artifact detected" in fact for fact in result.facts)
    assert any("tshark protocol hierarchy" in fact for fact in result.facts)


def test_forensics_rejects_parent_traversal_artifact_path(tmp_path: Path) -> None:
    triage = {
        "files": [
            {
                "path": "../outside.txt",
                "strings": [{"value": "flag{outside_forensics}", "offset": 0}],
                "indicators": [],
                "tool_results": [],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert any("escapes run_dir" in fact for fact in result.facts)
    assert not (tmp_path / "solve.py").exists()


def test_forensics_rejects_tool_output_only_outside_artifact(tmp_path: Path) -> None:
    safe = tmp_path / "files" / "image.png"
    safe.parent.mkdir()
    _write_png(safe, "no flag")
    outside = tmp_path.parent / "outside-exiftool.stdout"
    outside.write_text("Comment: flag{outside_tool}\n", encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(safe),
                "magic": "PNG image",
                "strings": [],
                "indicators": [],
                "tool_results": [{"tool": "exiftool", "stdout_artifact": str(outside)}],
            }
        ]
    }

    result = asyncio.run(
        ForensicsSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert "flag{outside_tool}" not in result.model_dump_json()
    assert not (tmp_path / "solve.py").exists()
