from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ctf_agent.ingestion.downloader import (
    DownloadSafetyError,
    filename_from_response,
    safe_destination,
    sanitize_filename,
)
from ctf_agent.platforms.base import Verdict, parse_submission_verdict
from ctf_agent.platforms.ctfd import extract_ctfd_challenge_id


def test_ctfd_challenge_id_parsing() -> None:
    assert extract_ctfd_challenge_id("https://ctf.test/challenges/web-warmup-123") == 123
    assert extract_ctfd_challenge_id("https://ctf.test/challenges/456") == 456


def test_verdict_parser_accepts_ctfd_success_payload() -> None:
    result = parse_submission_verdict(
        {"success": True, "data": {"status": "correct", "message": "Solved"}}
    )

    assert result.verdict is Verdict.ACCEPTED


def test_verdict_parser_rejects_wrong_answers() -> None:
    result = parse_submission_verdict({"data": "incorrect flag"})

    assert result.verdict is Verdict.WRONG


def test_filename_from_content_disposition_is_sanitized() -> None:
    headers = httpx.Headers({"content-disposition": 'attachment; filename="../../flag.zip"'})

    assert filename_from_response("https://ctf.test/files/1", headers) == "flag.zip"


def test_sanitize_filename_rejects_dotdot_extension_component() -> None:
    with pytest.raises(DownloadSafetyError):
        sanitize_filename("..")


def test_safe_destination_stays_under_directory(tmp_path: Path) -> None:
    target = safe_destination(tmp_path, "../payload.bin")

    assert target.parent == tmp_path.resolve()
    assert target.name == "payload.bin"
