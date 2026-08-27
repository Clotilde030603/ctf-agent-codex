"""Independent verification abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .candidate import FlagCandidate
from .flag_gate import FlagGate, SubmissionBudget
from .replay import ReplayResult, replay_solver


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    accepted: bool
    reason: str
    candidate: FlagCandidate
    replay: ReplayResult | None = None


class IndependentVerifier(Protocol):
    def verify(self, candidate: object) -> VerificationOutcome:
        """Return an independent verification outcome."""


@dataclass(slots=True)
class GateVerifier:
    gate: FlagGate
    budget: SubmissionBudget | None = None

    def verify(self, candidate: object) -> VerificationOutcome:
        decision = self.gate.evaluate(candidate, self.budget)
        return VerificationOutcome(decision.allowed, decision.reason, decision.candidate)


@dataclass(slots=True)
class ReplayVerifier:
    gate: FlagGate
    solver_path: Path
    flag_regex: str | None = None
    timeout_seconds: float = 30.0

    def verify(self, candidate: object) -> VerificationOutcome:
        decision = self.gate.evaluate(candidate)
        if not decision.allowed:
            return VerificationOutcome(False, decision.reason, decision.candidate)

        replay = replay_solver(
            self.solver_path,
            expected_flag=decision.candidate.normalized_value,
            flag_regex=self.flag_regex or self.gate.policy.regex,
            timeout_seconds=self.timeout_seconds,
        )
        if not replay.success:
            return VerificationOutcome(
                False,
                "fresh subprocess replay did not reproduce candidate",
                decision.candidate,
                replay,
            )
        return VerificationOutcome(
            True,
            "fresh subprocess replay reproduced candidate",
            decision.candidate,
            replay,
        )
