from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from pathlib import Path

from ctf_agent.config import Settings
from ctf_agent.schemas import Challenge, FlagPolicy, RunState
from ctf_agent.specialists.crypto import CryptoSpecialist
from ctf_agent.specialists.forensics import ForensicsSpecialist
from ctf_agent.specialists.web import StaticWebSpecialist
from ctf_agent.workflow import AutonomousWorkflow


def test_workflow_routes_supported_static_categories() -> None:
    assert isinstance(
        AutonomousWorkflow._category_specialist(
            {"classification": {"primary_category": "crypto-binary"}}
        ),
        CryptoSpecialist,
    )
    assert isinstance(
        AutonomousWorkflow._category_specialist(
            {"classification": {"primary_category": "forensics"}}
        ),
        ForensicsSpecialist,
    )
    assert isinstance(
        AutonomousWorkflow._category_specialist(
            {"classification": {"primary_category": "web"}}
        ),
        StaticWebSpecialist,
    )


def test_static_crypto_specialist_is_connected_to_solve_workflow(tmp_path: Path) -> None:
    workflow = AutonomousWorkflow(
        Settings(
            backend="static",
            runs_dir=tmp_path / "runs",
            allow_local_reproduction=True,
        )
    )
    context = workflow.controller().create_run(
        "https://ctf.test/challenges/crypto", auto_submit=False, writeup=False
    )
    context.values["challenge"] = Challenge(
        id="crypto",
        url="https://ctf.test/challenges/crypto",
        title="Encoded Fixture",
        category="crypto-binary",
        flag_policy=FlagPolicy(pattern=r"flag\{[^{}]+\}"),
    )
    encoded = base64.b64encode(b"flag{category_workflow}").decode()
    source = context.record.run_dir / "files" / "payload.txt"
    source.write_text(encoded, encoding="utf-8")
    triage = {
        "classification": {
            "primary_category": "crypto-binary",
            "evidence": [{"reason": "base64-like token"}],
        },
        "files": [
            {
                "path": str(source),
                "relative_path": "files/payload.txt",
                "strings": [{"value": encoded, "offset": 0}],
                "indicators": [],
                "tool_results": [],
            }
        ],
    }
    context.values["triage"] = triage
    (context.record.run_dir / "triage.json").write_text(json.dumps(triage))

    asyncio.run(workflow.plan(context))
    outcome = asyncio.run(workflow.solve(context))

    assert outcome.target is RunState.VERIFY
    assert outcome.payload["stop_reason"] == "category_crypto-deterministic"
    completed = subprocess.run(
        ["python3", "solve.py"],
        cwd=context.record.run_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.strip() == "flag{category_workflow}"
