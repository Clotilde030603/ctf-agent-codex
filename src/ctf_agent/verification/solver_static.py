"""Static checks for solver hardcoded flag candidates."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from .candidate import FlagCandidate


@dataclass(frozen=True, slots=True)
class SolverHardcodeCheck:
    hardcoded: bool
    reason: str
    encoding: str | None = None


@dataclass(frozen=True, slots=True)
class SolverStaticAnalyzer:
    solver_path: Path

    def detect_hardcoded_candidate(self, candidate_value: object) -> SolverHardcodeCheck:
        candidate = FlagCandidate.from_schema(candidate_value)
        value = candidate.normalized_value
        if not value:
            return SolverHardcodeCheck(False, "empty candidate cannot be hardcode-scanned")
        if not self.solver_path.is_file():
            return SolverHardcodeCheck(False, "solver file is missing")

        data = self.solver_path.read_bytes()
        for encoding, needle in _candidate_needles(value):
            if needle in data:
                return SolverHardcodeCheck(
                    True,
                    f"solver contains {encoding} candidate literal",
                    encoding,
                )
        return SolverHardcodeCheck(False, "solver contains no raw/base64/hex candidate literal")


def _candidate_needles(value: str) -> tuple[tuple[str, bytes], ...]:
    raw = value.encode("utf-8")
    b64 = base64.b64encode(raw)
    b64_unpadded = b64.rstrip(b"=")
    urlsafe_b64 = base64.urlsafe_b64encode(raw)
    urlsafe_b64_unpadded = urlsafe_b64.rstrip(b"=")
    hex_lower = raw.hex().encode("ascii")
    hex_upper = raw.hex().upper().encode("ascii")
    needles = (
        ("raw", raw),
        ("base64", b64),
        ("base64", b64_unpadded),
        ("base64url", urlsafe_b64),
        ("base64url", urlsafe_b64_unpadded),
        ("hex", hex_lower),
        ("hex", hex_upper),
    )
    return tuple((encoding, needle) for encoding, needle in needles if needle)
