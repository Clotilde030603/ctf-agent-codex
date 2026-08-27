from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path

from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.crypto import CryptoSpecialist


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        id="H1",
        claim="crypto-binary encoded flag",
        expected_signal="decoded flag",
        cost="low",
        confidence=0.7,
        kill_condition="no deterministic decode",
        success_condition="candidate",
    )


def test_crypto_supports_routes() -> None:
    specialist = CryptoSpecialist()
    assert specialist.supports("crypto-binary")
    assert specialist.supports("single-byte xor clue")
    assert not specialist.supports("forensics image")


def test_crypto_recovers_base64_hex_and_writes_reproducible_solver(tmp_path: Path) -> None:
    encoded = base64.b64encode(b"prefix flag{base64_ok} suffix").decode()
    hexed = b"flag{hex_ok}".hex()
    source = tmp_path / "files" / "crypto.txt"
    source.parent.mkdir()
    source.write_text(f"b64={encoded} hex={hexed}\n", encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(source),
                "strings": [{"value": f"b64={encoded} hex={hexed}", "offset": 3}],
                "indicators": [],
            }
        ]
    }
    run_dir = tmp_path

    result = asyncio.run(
        CryptoSpecialist().solve(_hypothesis(), {"run_dir": str(run_dir), "triage": triage})
    )

    assert result.status == "confirmed"
    assert {candidate.value for candidate in result.flag_candidates} == {
        "flag{base64_ok}",
        "flag{hex_ok}",
    }
    assert result.reproduction_command == "python3 solve.py"
    completed = subprocess.run(
        ["python3", "solve.py"],
        cwd=run_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "flag{base64_ok}" in completed.stdout
    assert "flag{hex_ok}" in completed.stdout
    solve_source = (run_dir / "solve.py").read_text(encoding="utf-8")
    assert "flag{base64_ok}" not in solve_source
    assert "flag{hex_ok}" not in solve_source
    assert encoded not in solve_source
    assert hexed not in solve_source

    source.write_text("b64=Zm9vCg== hex=626172\n", encoding="utf-8")
    altered = subprocess.run(
        ["python3", "solve.py"],
        cwd=run_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "flag{base64_ok}" not in altered.stdout
    assert "flag{hex_ok}" not in altered.stdout


def test_crypto_recovers_single_byte_xor_hex(tmp_path: Path) -> None:
    key = 0x23
    raw = bytes(byte ^ key for byte in b"flag{xor_ok}")
    source = tmp_path / "files" / "xor.txt"
    source.parent.mkdir()
    source.write_text(raw.hex() + "\n", encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(source),
                "strings": [{"value": raw.hex(), "offset": 0}],
                "indicators": [],
            }
        ]
    }

    result = asyncio.run(
        CryptoSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "confirmed"
    candidate = result.flag_candidates[0]
    assert candidate.value == "flag{xor_ok}"
    assert "xor key 0x23" in candidate.derivation
    solve_source = (tmp_path / "solve.py").read_text(encoding="utf-8")
    assert "flag{xor_ok}" not in solve_source
    assert raw.hex() not in solve_source


def test_crypto_missing_signal_is_inconclusive_not_fake_success(tmp_path: Path) -> None:
    triage = {"files": [{"path": "notes.txt", "strings": [{"value": "nothing useful"}]}]}

    result = asyncio.run(
        CryptoSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert not result.flag_candidates
    assert result.next_action
    assert not (tmp_path / "solve.py").exists()


def test_crypto_rejects_parent_traversal_artifact_path(tmp_path: Path) -> None:
    encoded = base64.b64encode(b"flag{outside_crypto}").decode()
    triage = {
        "files": [
            {
                "path": "../outside.txt",
                "strings": [{"value": encoded, "offset": 0}],
                "indicators": [],
            }
        ]
    }

    result = asyncio.run(
        CryptoSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert any("escapes run_dir" in fact for fact in result.facts)
    assert not (tmp_path / "solve.py").exists()


def test_crypto_rejects_absolute_outside_artifact_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-crypto.txt"
    outside.write_text(base64.b64encode(b"flag{outside_crypto}").decode(), encoding="utf-8")
    triage = {
        "files": [
            {
                "path": str(outside),
                "strings": [{"value": outside.read_text(encoding="utf-8"), "offset": 0}],
                "indicators": [],
            }
        ]
    }

    result = asyncio.run(
        CryptoSpecialist().solve(_hypothesis(), {"run_dir": str(tmp_path), "triage": triage})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert any("outside run_dir" in fact for fact in result.facts)
