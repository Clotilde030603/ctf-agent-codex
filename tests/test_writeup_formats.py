from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.evidence.manifest import EvidenceManifest
from ctf_agent.writeup.generator import WriteupGenerator
from ctf_agent.writeup.validator import WriteupValidator


def _write_run(run_dir: Path) -> None:
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir()
    proof = evidence_dir / "02-exploit-proof.html"
    proof.write_text("<pre>flag{public_secret}</pre>", encoding="utf-8")

    manifest = EvidenceManifest(run_id="formats")
    manifest.add_file(
        proof,
        root=run_dir,
        label="exploit-proof-transcript",
        media_type="text/html",
        source="solver-replay",
        redacted=True,
        producer="terminal-renderer",
        command="python3 solve.py",
        exit_code=0,
        model="gpt-test",
        tool="python",
        event_id=7,
        metadata={"screenshot_status": "playwright-unavailable"},
    )
    manifest.add_capture_failure(
        "exploit-proof-png",
        stage="EVIDENCE",
        reason="playwright-unavailable",
        producer="terminal-renderer",
        command="render terminal png",
        tool="playwright",
        event_id=8,
    )
    manifest.add_event("VERIFY", "flag replay succeeded", flag="flag{public_secret}", accepted=True)
    manifest.save(evidence_dir / "manifest.json")

    (run_dir / "challenge.json").write_text(
        json.dumps(
            {
                "title": "Formats",
                "category": "misc",
                "points": 50,
                "url": "https://ctf.example/challenges/2",
                "description": "Format coverage",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "triage.json").write_text(json.dumps({"files": ["proof.txt"]}), encoding="utf-8")
    (run_dir / "hypotheses.json").write_text(json.dumps([{"name": "proof"}]), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "stage": "VERIFY",
                "message": "flag replay succeeded",
                "data": {"flag": "flag{public_secret}", "accepted": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "solve.py").write_text("print('flag{public_secret}')\n", encoding="utf-8")


def test_generate_all_redacts_public_outputs_and_writes_provenance(tmp_path: Path) -> None:
    _write_run(tmp_path)

    outputs = WriteupGenerator().generate_all(tmp_path, redact_flags=True)
    validation = WriteupValidator().validate_all(tmp_path)

    markdown = outputs.markdown_path.read_text(encoding="utf-8")
    html = outputs.html_path.read_text(encoding="utf-8")
    provenance = json.loads(outputs.provenance_path.read_text(encoding="utf-8"))

    assert validation.ok, validation.errors
    assert "flag{public_secret}" not in markdown
    assert "flag{public_secret}" not in html
    assert "flag{public_secret}" not in json.dumps(provenance)
    assert "[REDACTED flag:" in markdown
    assert "flag-sha256:" in provenance["flag_reference"]["reference"]
    assert provenance["path"] == "provenance.json"
    assert {item["path"] for item in provenance["generated_outputs"]} == {
        "writeup.md",
        "writeup.html",
    }
    assert provenance["capture_failures"][0]["reason"] == "playwright-unavailable"
    assert provenance["evidence_entries"][0]["producer"] == "terminal-renderer"


def test_generate_all_private_outputs_keep_ledger_backed_flag(tmp_path: Path) -> None:
    _write_run(tmp_path)

    outputs = WriteupGenerator().generate_all(tmp_path, redact_flags=False)
    validation = WriteupValidator().validate_all(tmp_path)

    assert validation.ok, validation.errors
    assert "flag{public_secret}" in outputs.markdown_path.read_text(encoding="utf-8")
    assert "flag{public_secret}" in outputs.html_path.read_text(encoding="utf-8")


def test_redaction_covers_all_template_context_fields(tmp_path: Path) -> None:
    _write_run(tmp_path)
    challenge = json.loads((tmp_path / "challenge.json").read_text())
    challenge["description"] = "description leaks flag{public_secret}"
    (tmp_path / "challenge.json").write_text(json.dumps(challenge))
    (tmp_path / "triage.json").write_text(
        json.dumps({"fact": "triage leaks flag{public_secret}"})
    )
    (tmp_path / "hypotheses.json").write_text(
        json.dumps([{"claim": "hypothesis leaks flag{public_secret}"}])
    )

    outputs = WriteupGenerator().generate_all(tmp_path, redact_flags=True)
    validation = WriteupValidator().validate_all(tmp_path)

    assert validation.ok, validation.errors
    assert "flag{public_secret}" not in outputs.markdown_path.read_text()
    assert "flag{public_secret}" not in outputs.html_path.read_text()
