from __future__ import annotations

import hashlib
from pathlib import Path

from ctf_agent.budget import ModelBudgetBroker
from ctf_agent.budget_types import (
    ArtifactProgress,
    BudgetPolicy,
    CandidateReceipt,
    ProgressEvidence,
)
from ctf_agent.lanes import ProvenancedFact
from ctf_agent.schemas import RunRecord
from ctf_agent.state import StateStore


def _broker(tmp_path: Path, **options: int) -> ModelBudgetBroker:
    store = StateStore(tmp_path / "state.db")
    run_id = "budget-progress-run"
    store.create(
        RunRecord(
            run_id=run_id,
            challenge_url="https://ctf.test/c/budget-progress",
            run_dir=tmp_path,
        )
    )
    return store.model_budget_broker(run_id, BudgetPolicy(**options))


def test_budget_untrusted_model_fact_cannot_grant_extension(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=2,
        hard_limit=3,
        verifier_floor=1,
        max_extensions=1,
    )
    claim = "untrusted model claim"
    untrusted = ProvenancedFact(
        fact=claim,
        source="model",
        evidence_sha256=hashlib.sha256(claim.encode()).hexdigest(),
        status="untrusted",
        sequence=1,
    )
    forged = untrusted.model_copy(
        update={"status": "validated", "evidence_sha256": "a" * 64}
    )

    assert broker.extend(ProgressEvidence(facts=(untrusted,))) == 0
    assert broker.extend(ProgressEvidence(facts=(forged,))) == 0
    assert broker.snapshot().extended == 0


def test_budget_candidate_receipt_requires_valid_novel_identity(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=2,
        hard_limit=4,
        verifier_floor=1,
        max_extensions=2,
    )
    receipt = CandidateReceipt(hashlib.sha256(b"candidate").hexdigest())

    assert broker.extend(ProgressEvidence(candidates=(CandidateReceipt("invalid"),))) == 0
    assert broker.extend(ProgressEvidence(candidates=(receipt,))) == 1
    assert broker.extend(ProgressEvidence(candidates=(receipt,))) == 0


def test_budget_replayed_progress_identity_cannot_extend_twice(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=2,
        hard_limit=4,
        verifier_floor=1,
        max_extensions=2,
    )
    artifact = tmp_path / "controller-proof.bin"
    artifact.write_bytes(b"bounded controller proof")
    content_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    forged = ProgressEvidence(artifacts=(ArtifactProgress(artifact, "a" * 64),))
    evidence = ProgressEvidence(
        artifacts=(ArtifactProgress(artifact, content_sha256),)
    )

    mismatch = broker.extend(forged)
    first = broker.extend(evidence)
    replay = broker.extend(evidence)

    assert mismatch == 0
    assert first == 1
    assert replay == 0
    assert broker.snapshot().extended == 1


def test_budget_extension_is_evidence_bounded(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=2,
        hard_limit=4,
        verifier_floor=1,
        max_extensions=1,
        extension_size=3,
    )
    claim = "new solver provenance"
    verified = ProvenancedFact(
        fact=claim,
        source="command",
        evidence_sha256=hashlib.sha256(claim.encode()).hexdigest(),
        status="validated",
        sequence=1,
    )
    evidence = ProgressEvidence(facts=(verified,))

    assert broker.extend(evidence) == 2
    assert broker.extend(evidence) == 0
    assert broker.snapshot().extended == 2


def test_budget_hard_limit_cannot_extend(tmp_path: Path) -> None:
    broker = _broker(
        tmp_path,
        initial_limit=3,
        hard_limit=3,
        verifier_floor=1,
        max_extensions=2,
        extension_size=1,
    )
    claim = "solver produced new provenance evidence"
    verified = ProvenancedFact(
        fact=claim,
        source="command",
        evidence_sha256=hashlib.sha256(claim.encode()).hexdigest(),
        status="validated",
        sequence=1,
    )

    assert broker.extend(ProgressEvidence(facts=(verified,))) == 0
    assert broker.snapshot().extended == 0
