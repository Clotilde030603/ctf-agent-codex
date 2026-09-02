from __future__ import annotations

import json
import stat
from functools import partial
from pathlib import Path
from typing import Any

import anyio

from ctf_agent.context_projector import ContextProjector, render_codex_prompt
from ctf_agent.models.base import ModelRequest
from ctf_agent.models.codex import CodexCliBackend


def _append_event(
    events: list[tuple[str, dict[str, Any]]],
    event_type: str,
    payload: dict[str, Any],
) -> None:
    events.append((event_type, dict(payload)))


def test_full_challenge_contract_is_non_borrowable_under_projection_pressure() -> None:
    # Given: controller-ingested challenge fields plus optional oversized model context.
    challenge = {
        "id": "challenge-8",
        "url": "https://ctf.test/challenges/8",
        "event": "fixture-event",
        "title": "contract-title",
        "description": "contract-description",
        "category": "forensics",
        "points": 350,
        "flag_policy": {
            "pattern": "flag-pattern",
            "prefix": "flag-prefix",
            "case_sensitive": False,
            "examples": [],
        },
        "attachment_urls": ["https://ctf.test/files/evidence.bin"],
        "service_hosts": ["service.ctf.test"],
        "metadata": {"required_input": "capture.pcap", "protocol": "tcp"},
    }
    request = ModelRequest(
        role="planner",
        prompt="plan",
        context={
            "challenge": challenge,
            "flag_policy": challenge["flag_policy"],
            "service_hosts": challenge["service_hosts"],
            "hypothesis": {"id": "H1"},
            "triage": {"blob": "x" * 100_000},
        },
    )

    # When: optional context must yield to the hard byte budget.
    projection = ContextProjector(12_000).project(request, render_codex_prompt)

    # Then: every machine-consumed challenge field remains trusted and included.
    projected = next(item for item in projection.sections if item.section_id == "challenge")
    decision = next(
        item for item in projection.manifest.sections if item.section_id == "challenge"
    )
    assert projected.content == challenge
    assert projected.mandatory is True
    assert projected.trust_label == "trusted_runtime"
    assert decision.action == "included"
    assert decision.mandatory is True


def test_only_controller_validated_provenanced_facts_are_non_borrowable() -> None:
    # Given: one controller-observed fact and one raw model claim in a lane checkpoint.
    validated = {
        "fact": "controller-observed fact",
        "source": "command",
        "artifact": "artifacts/lanes/H1/step.stdout.txt",
        "command": ["file", "files/payload.bin"],
        "evidence_sha256": "a" * 64,
        "status": "validated",
        "sequence": 1,
    }
    raw_claim = {
        "fact": "raw model claim",
        "source": "model",
        "artifact": None,
        "command": [],
        "evidence_sha256": "b" * 64,
        "status": "untrusted",
        "sequence": 2,
    }
    request = ModelRequest(
        role="solver",
        prompt="solve",
        context={
            "challenge": {
                "title": "fact-contract",
                "category": "rev",
                "flag_policy": {"pattern": "flag-pattern"},
                "service_hosts": [],
                "metadata": {"required_input": "payload.bin"},
            },
            "lane_checkpoint": {
                "step_index": 2,
                "facts": [validated, raw_claim],
                "verified_facts": [validated["fact"], raw_claim["fact"]],
            },
            "triage": {"blob": "x" * 100_000},
        },
    )

    # When: the checkpoint is projected under pressure.
    projection = ContextProjector(12_000).project(request, render_codex_prompt)

    # Then: validated provenance is mandatory; the raw checkpoint remains optional data.
    facts = next(item for item in projection.sections if item.section_id == "verified_facts")
    fact_decision = next(
        item for item in projection.manifest.sections if item.section_id == "verified_facts"
    )
    checkpoint_decision = next(
        item for item in projection.manifest.sections if item.section_id == "lane_checkpoint"
    )
    assert facts.content == [validated]
    assert facts.trust_label == "trusted_runtime"
    assert facts.mandatory is True
    assert fact_decision.action == "included"
    assert checkpoint_decision.mandatory is False
    assert checkpoint_decision.trust_label == "untrusted_data"


def test_codex_emits_deterministic_sanitized_projection_item_events(tmp_path: Path) -> None:
    # Given: two identical projected calls with credential-shaped optional data.
    executable = tmp_path / "fake_codex.py"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "sys.stdin.buffer.read()\n"
        "final=pathlib.Path(args[args.index('--output-last-message')+1])\n"
        "final.write_text(json.dumps({'content':'ok'}))\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    request = ModelRequest(
        role="solver",
        prompt="solve",
        context={
            "challenge": {
                "title": "event-contract",
                "category": "misc",
                "flag_policy": {"pattern": "flag-pattern"},
                "service_hosts": [],
                "metadata": {"required_input": "input.bin"},
            },
            "hypothesis": {"id": "H1"},
            "api_key": "event-secret-value",
            "triage": {"blob": "x" * 100_000},
            **{f"optional_{index}": "x" * 10_000 for index in range(80)},
        },
    )
    observed: list[list[tuple[str, dict[str, Any]]]] = []

    # When: both calls complete through the Codex event observer seam.
    for _ in range(2):
        events: list[tuple[str, dict[str, Any]]] = []
        backend = CodexCliBackend(
            executable=str(executable),
            cwd=tmp_path,
            max_prompt_bytes=12_000,
            timeout_seconds=5,
            projection_event_observer=partial(_append_event, events),
        )
        anyio.run(backend.complete, request)
        observed.append(events)

    # Then: every section decision has stable structural metadata and no content.
    first_items = [payload for event, payload in observed[0] if event == "context.projection_item"]
    second_items = [payload for event, payload in observed[1] if event == "context.projection_item"]
    assert first_items == second_items
    assert len(first_items) > 0
    assert {item["action"] for item in first_items} >= {"included", "summarized", "omitted"}
    for item in first_items:
        assert set(item) == {
            "section_id",
            "action",
            "original_bytes",
            "final_bytes",
            "provenance",
            "trust_label",
            "sha256",
            "truncation_marker",
        }
        assert len(str(item["sha256"])) == 64
    serialized = json.dumps(observed, sort_keys=True)
    assert "event-secret-value" not in serialized
    assert [event for event, _ in observed[0]][0] == "context.projection_started"
    assert [event for event, _ in observed[0]][-1] == "context.projection_completed"
