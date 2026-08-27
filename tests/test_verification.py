from __future__ import annotations

from pathlib import Path

from ctf_agent.schemas import FlagCandidate as SchemaFlagCandidate
from ctf_agent.schemas import FlagPolicy as SchemaFlagPolicy
from ctf_agent.verification.candidate import FlagCandidate, Provenance
from ctf_agent.verification.flag_gate import FlagGate, FlagPolicy, SubmissionBudget
from ctf_agent.verification.independent_review import GateVerifier, ReplayVerifier
from ctf_agent.verification.replay import replay_solver


def _candidate(flag: str) -> FlagCandidate:
    return FlagCandidate(
        value=flag,
        provenance=(
            Provenance(
                source="solver:web",
                method="derived from exploit output",
                command=("python", "solve.py"),
            ),
        ),
    )


def test_flag_gate_accepts_format_with_actionable_provenance() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}", prefix="CTF{"))

    decision = gate.evaluate(_candidate("CTF{real_solution_123}"))

    assert decision.allowed is True
    assert decision.reason == "accepted by deterministic gate"


def test_flag_gate_rejects_placeholders_and_samples() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}", prefix="CTF{"))

    decision = gate.evaluate(_candidate("CTF{example}"))

    assert decision.allowed is False
    assert "placeholder" in decision.reason


def test_flag_gate_requires_actionable_provenance() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}"))

    decision = gate.evaluate("CTF{real_but_unproven}")

    assert decision.allowed is False
    assert "provenance" in decision.reason


def test_rejected_candidate_is_blocked() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}"))
    gate.rejected_candidates.add("CTF{wrong_once}")

    decision = gate.evaluate(_candidate("CTF{wrong_once}"))

    assert decision.allowed is False
    assert "previously" in decision.reason


def test_submission_budget_blocks_when_exhausted() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}"))
    budget = SubmissionBudget(max_attempts=1)

    first = gate.evaluate(_candidate("CTF{first_try}"), budget)
    assert first.allowed is True
    assert gate.reserve_submission(first, budget) is True
    second = gate.evaluate(_candidate("CTF{second_try}"), budget)

    assert second.allowed is False
    assert second.reason == "submission budget exhausted"


def test_existing_schema_candidate_and_policy_are_accepted() -> None:
    schema_policy = SchemaFlagPolicy(pattern=r"CTF\{[A-Za-z0-9_]+\}", prefix="CTF{")
    schema_candidate = SchemaFlagCandidate(
        value="CTF{schema_candidate}",
        source_artifact="files/chal.bin",
        source_location="stdout line 1",
        derivation=["ran solver", "parsed stdout"],
        solver_command="python solve.py",
    )

    decision = FlagGate(schema_policy).evaluate(schema_candidate)

    assert decision.allowed is True
    assert decision.candidate.provenance[0].artifact == Path("files/chal.bin")


def test_replay_solver_runs_in_fresh_subprocess(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('CTF{replayed_flag}')\n", encoding="utf-8")

    result = replay_solver(solver, expected_flag="CTF{replayed_flag}", timeout_seconds=5)

    assert result.success is True
    assert result.returncode == 0
    assert result.matched_flag == "CTF{replayed_flag}"


def test_replay_verifier_rejects_when_solver_does_not_reproduce(tmp_path: Path) -> None:
    solver = tmp_path / "solve.py"
    solver.write_text("print('CTF{different_flag}')\n", encoding="utf-8")
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}"))

    outcome = ReplayVerifier(gate, solver, timeout_seconds=5).verify(
        _candidate("CTF{expected_flag}")
    )

    assert outcome.accepted is False
    assert outcome.replay is not None
    assert outcome.replay.success is False


def test_independent_gate_verifier_implements_protocol() -> None:
    gate = FlagGate(FlagPolicy(regex=r"CTF\{[A-Za-z0-9_]+\}"))

    outcome = GateVerifier(gate).verify(_candidate("CTF{verified_by_gate}"))

    assert outcome.accepted is True
    assert outcome.candidate.normalized_value == "CTF{verified_by_gate}"
