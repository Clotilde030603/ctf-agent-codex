"""Low-cost specialist for deterministic flag-bearing artifact signals."""

from __future__ import annotations

import json
from pathlib import Path

from ctf_agent.schemas import FlagCandidate, Hypothesis, SpecialistResult


class ArtifactSignalSpecialist:
    """Turn deterministic triage indicators into a reproducible solver.

    This is intentionally narrow: difficult challenges are delegated to model-backed
    specialists, while obvious artifact-derived candidates avoid unnecessary model calls.
    """

    name = "artifact-signal"

    def supports(self, _claim: str) -> bool:
        return True

    async def solve(
        self, hypothesis: Hypothesis, context: dict[str, object]
    ) -> SpecialistResult:
        run_dir = Path(str(context["run_dir"]))
        triage = context.get("triage")
        triage_data = triage if isinstance(triage, dict) else {}
        candidates: list[FlagCandidate] = []
        seen: set[str] = set()
        for scanned in triage_data.get("files", []):
            if not isinstance(scanned, dict):
                continue
            for indicator in scanned.get("indicators", []):
                if not isinstance(indicator, dict) or indicator.get("kind") != "flag-like":
                    continue
                value = str(indicator.get("value", ""))
                if not value or value in seen:
                    continue
                seen.add(value)
                location = (
                    f"offset {indicator.get('offset')}"
                    if indicator.get("offset") is not None
                    else f"line {indicator.get('line', 'unknown')}"
                )
                candidates.append(
                    FlagCandidate(
                        value=value,
                        source_artifact=str(indicator.get("artifact_path") or scanned.get("path")),
                        source_location=location,
                        derivation=[
                            "deterministic recursive scan",
                            "flag-like byte pattern with recorded location",
                        ],
                        solver_command="python3 solve.py",
                        format_match=True,
                        confidence=0.75,
                    )
                )
        if candidates:
            self._write_solver(run_dir)
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="confirmed",
                facts=[f"found {len(candidates)} flag-like artifact signal(s)"],
                artifacts=[candidate.source_artifact for candidate in candidates],
                commands=["python3 solve.py"],
                reproduction_command="python3 solve.py",
                flag_candidates=candidates,
                next_action="independent verification",
                confidence=max(candidate.confidence for candidate in candidates),
            )
        return SpecialistResult(
            hypothesis_id=hypothesis.id,
            status="inconclusive",
            facts=[],
            next_action="use a category-specific model specialist",
            confidence=0.1,
        )

    @staticmethod
    def _write_solver(run_dir: Path) -> None:
        pattern = r"[A-Za-z0-9_.-]+\{[^{}\r\n]{1,256}\}"
        source = f'''#!/usr/bin/env python3
"""Reproduce candidates from the preserved challenge artifacts."""
from pathlib import Path
import re

PATTERN = re.compile({json.dumps(pattern)}.encode())
for path in sorted(Path("files").rglob("*")):
    if not path.is_file():
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    for match in PATTERN.finditer(data):
        print(match.group().decode(errors="replace"))
'''
        solve_path = run_dir / "solve.py"
        solve_path.write_text(source, encoding="utf-8")
        solve_path.chmod(0o755)
