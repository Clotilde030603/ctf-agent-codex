from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.evidence.manifest import EvidenceManifest
from ctf_agent.evidence.sanitizer import SecretSanitizer
from ctf_agent.evidence.terminal_render import TerminalRenderer
from ctf_agent.writeup.generator import WriteupGenerator
from ctf_agent.writeup.validator import WriteupValidator


def test_sanitizer_redacts_auth_material_without_redacting_flags() -> None:
    text = (
        "Authorization: Bearer superSecretToken1234567890ABCDEFG\n"
        "password=hunter2\n"
        "https://ctf.example/chal?token=abc123&view=1\n"
        "flag{this_is_the_ctf_answer}\n"
    )

    result = SecretSanitizer().sanitize(text)

    assert "[REDACTED]" in result.text
    assert "hunter2" not in result.text
    assert "superSecretToken" not in result.text
    assert "flag{this_is_the_ctf_answer}" in result.text
    assert "view=1" in result.text
    assert result.redacted


def test_terminal_renderer_creates_sanitized_html_with_png_fallback(tmp_path: Path) -> None:
    result = TerminalRenderer().render(
        "token=abc123\nflag{ok}\n",
        tmp_path,
        command="python3 solve.py --api-key secret-value",
        make_png=True,
    )

    html = result.html_path.read_text(encoding="utf-8")
    assert result.html_path.exists()
    assert "[REDACTED]" in html
    assert "flag{ok}" in html
    assert result.screenshot_status in {"created", "playwright-unavailable"}
    if result.png_path is not None:
        assert result.png_path.exists()


def test_manifest_hashes_and_writeup_validation(tmp_path: Path) -> None:
    run_dir = tmp_path
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir()
    proof = evidence_dir / "02-exploit-proof.html"
    proof.write_text("<pre>flag{verified}</pre>", encoding="utf-8")

    manifest = EvidenceManifest(run_id="demo")
    manifest.add_file(
        proof,
        root=run_dir,
        label="exploit proof",
        media_type="text/html",
        source="terminal",
        redacted=False,
    )
    manifest.add_event("VERIFY", "flag replay succeeded", flag="flag{verified}", accepted=True)
    manifest.save(evidence_dir / "manifest.json")

    (run_dir / "challenge.json").write_text(
        json.dumps(
            {
                "title": "Demo",
                "category": "misc",
                "points": 100,
                "url": "https://ctf.example/challenges/1",
                "description": "Demo challenge",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "triage.json").write_text(json.dumps({"files": ["chall.txt"]}), encoding="utf-8")
    (run_dir / "hypotheses.json").write_text(json.dumps([{"name": "strings"}]), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "stage": "VERIFY",
                "message": "flag replay succeeded",
                "data": {"flag": "flag{verified}", "accepted": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "solve.py").write_text("print('flag{verified}')\n", encoding="utf-8")

    writeup = WriteupGenerator().generate(run_dir)
    result = WriteupValidator().validate(run_dir, writeup)

    assert writeup.exists()
    assert result.ok, result.errors
    text = writeup.read_text(encoding="utf-8")
    assert "Demo" in text
    assert "evidence/02-exploit-proof.html" in text
    assert "flag{verified}" in text


def test_validator_rejects_unsupported_flag_claim(tmp_path: Path) -> None:
    run_dir = tmp_path
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir()
    EvidenceManifest(run_id="demo").save(evidence_dir / "manifest.json")
    writeup = (
        "# Demo\n\n## Challenge\n\n## Verified Flag\n\n"
        "`flag{unsupported}`\n\n## Evidence\n\n## Reproduction\n"
    )
    (run_dir / "writeup.md").write_text(
        writeup,
        encoding="utf-8",
    )

    result = WriteupValidator().validate(run_dir)

    assert not result.ok
    assert any("unsupported" in error for error in result.errors)
