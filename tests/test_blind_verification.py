from __future__ import annotations

import base64
from pathlib import Path

from ctf_agent.schemas import FlagCandidate as SchemaFlagCandidate
from ctf_agent.verification import BlindVerifier, FlagPolicy, ProvenanceVerifier
from ctf_agent.verification.solver_static import SolverStaticAnalyzer


def _policy() -> FlagPolicy:
    return FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}", prefix="CTF{")


def _candidate(
    value: str = "CTF{from_data}",
    *,
    source_artifact: str = "files/payload.txt",
    source_location: str = "offset 0",
) -> SchemaFlagCandidate:
    return SchemaFlagCandidate(
        value=value,
        source_artifact=source_artifact,
        source_location=source_location,
        derivation=["test fixture"],
        solver_command="python3 solve.py",
    )


def test_provenance_verifier_accepts_offset_and_line_bounds(tmp_path: Path) -> None:
    source = tmp_path / "files" / "payload.txt"
    source.parent.mkdir()
    source.write_text("first\nCTF{from_data}\n", encoding="utf-8")

    offset = ProvenanceVerifier(tmp_path).verify(_candidate(source_location="offset 6"))
    line = ProvenanceVerifier(tmp_path).verify(_candidate(source_location="files/payload.txt:2"))

    assert offset.accepted is True
    assert line.accepted is True


def test_provenance_verifier_rejects_missing_outside_and_bounds(tmp_path: Path) -> None:
    source = tmp_path / "files" / "payload.txt"
    source.parent.mkdir()
    source.write_text("one line\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-provenance.txt"
    outside.write_text("CTF{outside}", encoding="utf-8")

    missing = ProvenanceVerifier(tmp_path).verify(_candidate(source_artifact="files/missing.txt"))
    outside_result = ProvenanceVerifier(tmp_path).verify(
        _candidate(source_artifact=str(outside))
    )
    bad_line = ProvenanceVerifier(tmp_path).verify(_candidate(source_location="line 9"))
    bad_path_line = ProvenanceVerifier(tmp_path).verify(
        _candidate(source_location="files/payload.txt:9")
    )
    bad_offset = ProvenanceVerifier(tmp_path).verify(_candidate(source_location="offset 99"))

    assert missing.accepted is False
    assert "missing or outside" in missing.reason
    assert outside_result.accepted is False
    assert "missing or outside" in outside_result.reason
    assert bad_line.accepted is False
    assert "line" in bad_line.reason
    assert bad_path_line.accepted is False
    assert "line" in bad_path_line.reason
    assert bad_offset.accepted is False
    assert "offset" in bad_offset.reason


def test_solver_static_analyzer_rejects_raw_base64_and_hex_literals(tmp_path: Path) -> None:
    value = "CTF{hardcoded}"
    for content in (
        f"print({value!r})\n",
        f"print({base64.b64encode(value.encode()).decode()!r})\n",
        f"print({value.encode().hex()!r})\n",
    ):
        solver = tmp_path / "solve.py"
        solver.write_text(content, encoding="utf-8")

        result = SolverStaticAnalyzer(solver).detect_hardcoded_candidate(_candidate(value))

        assert result.hardcoded is True


def test_blind_verifier_accepts_data_dependent_solver(tmp_path: Path) -> None:
    payload = tmp_path / "files" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("CTF{from_data}\n", encoding="utf-8")
    (tmp_path / "solve.py").write_text(
        "from pathlib import Path\nprint(Path('files/payload.txt').read_text().strip())\n",
        encoding="utf-8",
    )

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(_candidate())

    assert outcome.accepted is True
    assert outcome.replay is not None
    assert outcome.replay.matched_flag == "CTF{from_data}"
    assert outcome.negative_control is not None
    assert outcome.negative_control.matched_flag is None


def test_blind_verifier_rejects_replay_mismatch(tmp_path: Path) -> None:
    payload = tmp_path / "files" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("CTF{from_data}\n", encoding="utf-8")
    (tmp_path / "solve.py").write_text(
        "from pathlib import Path\nprint(Path('files/payload.txt').read_text().strip())\n",
        encoding="utf-8",
    )

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(
        _candidate("CTF{different}", source_artifact="files/payload.txt")
    )

    assert outcome.accepted is False
    assert outcome.failure_stage == "replay"
    assert outcome.replay is not None
    assert outcome.replay.matched_flag == "CTF{from_data}"


def test_blind_verifier_rejects_hardcoded_solver(tmp_path: Path) -> None:
    payload = tmp_path / "files" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("CTF{from_data}\n", encoding="utf-8")
    (tmp_path / "solve.py").write_text("print('CTF{from_data}')\n", encoding="utf-8")

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(_candidate())

    assert outcome.accepted is False
    assert outcome.failure_stage == "hardcode"
    assert outcome.hardcode is not None
    assert outcome.hardcode.hardcoded is True


def test_blind_verifier_rejects_data_independent_obfuscated_solver(tmp_path: Path) -> None:
    payload = tmp_path / "files" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("CTF{from_data}\n", encoding="utf-8")
    (tmp_path / "solve.py").write_text(
        "parts = ['CTF', '{from', '_data}']\nprint(''.join(parts))\n",
        encoding="utf-8",
    )

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(_candidate())

    assert outcome.accepted is False
    assert outcome.failure_stage == "independent"
    assert outcome.negative_control is not None
    assert outcome.negative_control.matched_flag == "CTF{from_data}"


def test_blind_verifier_does_not_copy_candidate_bearing_workflow_metadata(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "files" / "payload.txt"
    payload.parent.mkdir()
    payload.write_text("no flag in the claimed source\n", encoding="utf-8")
    metadata = tmp_path / "artifacts" / "specialist-results.json"
    metadata.parent.mkdir()
    metadata.write_text('{"flag_candidates":[{"value":"CTF{metadata_leak}"}]}')
    (tmp_path / "solve.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "p=Path('artifacts/specialist-results.json')\n"
        "print(json.loads(p.read_text())['flag_candidates'][0]['value'])\n",
        encoding="utf-8",
    )

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(
        _candidate("CTF{metadata_leak}", source_artifact="files/payload.txt")
    )

    assert outcome.accepted is False
    assert outcome.failure_stage == "replay"
    assert outcome.replay is not None
    assert outcome.replay.matched_flag is None


def test_blind_verifier_rejects_metadata_claimed_as_provenance(tmp_path: Path) -> None:
    metadata = tmp_path / "artifacts" / "specialist-results.json"
    metadata.parent.mkdir()
    metadata.write_text('{"flag_candidates":[{"value":"CTF{metadata_claim}"}]}')
    (tmp_path / "solve.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "p=Path('artifacts/specialist-results.json')\n"
        "print(json.loads(p.read_text())['flag_candidates'][0]['value'])\n",
        encoding="utf-8",
    )

    outcome = BlindVerifier(tmp_path, _policy(), timeout_seconds=5).verify(
        _candidate(
            "CTF{metadata_claim}",
            source_artifact="artifacts/specialist-results.json",
        )
    )

    assert outcome.accepted is False
    assert outcome.failure_stage == "replay"
    assert outcome.replay is not None
    assert outcome.replay.matched_flag is None
