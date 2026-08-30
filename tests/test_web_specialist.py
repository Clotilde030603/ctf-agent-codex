from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ctf_agent.schemas import Hypothesis
from ctf_agent.specialists.web import StaticWebSpecialist


def hypothesis() -> Hypothesis:
    return Hypothesis(
        id="web-1",
        claim="web source analysis",
        expected_signal="route and flag candidate",
        cost="low",
        confidence=0.7,
        kill_condition="no static web facts",
        success_condition="candidate reproduced",
    )


def test_supports_web_routing_terms() -> None:
    specialist = StaticWebSpecialist()

    assert specialist.supports("web")
    assert specialist.supports("Flask route hypothesis")
    assert specialist.supports("HTTP API")
    assert not specialist.supports("crypto math")


def test_static_web_specialist_extracts_routes_boundaries_and_candidates(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    (files / "app.py").write_text(
        "\n".join(
            [
                "from flask import Flask, request, session",
                "app = Flask(__name__)",
                "@app.route('/admin', methods=['POST'])",
                "def admin():",
                "    token = request.form.get('csrf_token')",
                "    if session.get('user') == 'admin' and token:",
                "        return 'flag{' + 'static_web_win}'",
                "    return 'no'",
            ]
        ),
        encoding="utf-8",
    )

    result = asyncio.run(
        StaticWebSpecialist().solve(hypothesis(), {"run_dir": str(tmp_path)})
    )

    assert result.status == "confirmed"
    assert any("files/app.py:3 route POST /admin -> admin" in fact for fact in result.facts)
    assert any("files/app.py:5 parameter csrf_token" in fact for fact in result.facts)
    assert any("files/app.py:6 auth/session boundary" in fact for fact in result.facts)
    assert [candidate.value for candidate in result.flag_candidates] == ["flag{static_web_win}"]
    assert result.flag_candidates[0].source_location == "files/app.py:7"
    assert (tmp_path / "artifacts" / "web_solve.py").is_file()

    completed = subprocess.run(
        ["python3", "solve.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "flag{static_web_win}"


def test_static_web_specialist_reports_missing_capability_without_fake_success(
    tmp_path: Path,
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    (files / "app.js").write_text(
        "app.post('/login', (req, res) => res.send(req.body.name));",
        encoding="utf-8",
    )

    result = asyncio.run(
        StaticWebSpecialist().solve(hypothesis(), {"run_dir": str(tmp_path)})
    )

    assert result.status == "inconclusive"
    assert result.flag_candidates == []
    assert result.reproduction_command == ""
    assert any("route POST /login" in fact for fact in result.facts)
    assert any("missing capability" in fact for fact in result.facts)


def test_static_web_extracts_js_endpoints_graphql_and_websocket_urls(
    tmp_path: Path,
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    (files / "client.js").write_text(
        "fetch('/api/items');\n"
        "const q = `query LoadItems { items { id } }`;\n"
        "const ws = new WebSocket('wss://challenge.test/socket');\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        StaticWebSpecialist().solve(hypothesis(), {"run_dir": str(tmp_path)})
    )

    assert any("client endpoint /api/items" in fact for fact in result.facts)
    assert any("GraphQL query LoadItems" in fact for fact in result.facts)
    assert any("WebSocket URL wss://challenge.test/socket" in fact for fact in result.facts)
