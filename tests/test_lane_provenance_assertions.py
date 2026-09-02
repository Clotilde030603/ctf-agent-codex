from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.events import EventLedger
from ctf_agent.lanes import LaneCheckpoint, LaneCheckpointStore, LaneModelIdentity


def checkpoint(**updates: object) -> LaneCheckpoint:
    values: dict[str, object] = {
        "lane_id": "lane-a",
        "run_id": "run-a",
        "hypothesis_id": "H1",
        "hypothesis_revision": "r1",
        "category": "misc",
        "model_identity": LaneModelIdentity(
            specialist="solver",
            model="m1",
            effort="high",
            skill_sha256="s",
            capability_sha256="c",
            attachment_sha256="a",
        ),
        "hypothesis": "claim",
        "restatement": "claim",
        **updates,
    }
    return LaneCheckpoint.model_validate(values)


def test_model_fact_is_not_verified_and_revision_keeps_progress(tmp_path: Path) -> None:
    store = LaneCheckpointStore(tmp_path / "state.db")
    saved = store.save(checkpoint(verified_facts=("raw model fact",)))
    resumed, reset = store.resume_or_reset(
        checkpoint(hypothesis_revision="r2", hypothesis="revised")
    )
    assert "raw model fact" not in tuple(
        fact.fact for fact in saved.facts if fact.status == "validated"
    )
    assert reset is False
    assert resumed.step_index == saved.step_index
    assert resumed.verified_facts == saved.verified_facts


def test_lifecycle_events_have_explicit_values(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    ledger = EventLedger(database, tmp_path / "events.jsonl")
    store = LaneCheckpointStore(
        database,
        event_observer=lambda event_type, payload: ledger.append("run-a", event_type, payload),
    )
    created, _ = store.resume_or_reset(checkpoint())
    store.resume_or_reset(checkpoint())
    store.save(created.model_copy(update={"revision": 1, "next_action": "updated"}))
    events = ledger.list("run-a")
    assert events
    for event in events:
        payload = event["payload"]
        assert payload["lane_id"] == "lane-a"
        assert payload["reason"]
        assert isinstance(payload["revision"], int)
        assert isinstance(payload["workspace_generation"], int)
        assert "secret" not in json.dumps(payload)
