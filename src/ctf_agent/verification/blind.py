"""Blind solver verification without leaking the expected flag."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .candidate import FlagCandidate
from .flag_gate import FlagPolicy
from .provenance import ProvenanceCheck, ProvenanceVerifier
from .replay import ReplayResult, replay_solver
from .solver_static import SolverHardcodeCheck, SolverStaticAnalyzer

FailureStage = Literal["provenance", "hardcode", "replay", "independent"]


@dataclass(frozen=True, slots=True)
class BlindVerificationOutcome:
    accepted: bool
    reason: str
    candidate: FlagCandidate
    failure_stage: FailureStage | None = None
    provenance: ProvenanceCheck | None = None
    hardcode: SolverHardcodeCheck | None = None
    replay: ReplayResult | None = None
    negative_control: ReplayResult | None = None


@dataclass(frozen=True, slots=True)
class BlindVerifier:
    run_dir: Path
    flag_policy: FlagPolicy | object
    solver_path: Path | None = None
    timeout_seconds: float = 30.0

    def verify(self, candidate_value: object) -> BlindVerificationOutcome:
        candidate = FlagCandidate.from_schema(candidate_value)
        provenance = ProvenanceVerifier(self.run_dir).verify(candidate)
        if not provenance.accepted:
            return BlindVerificationOutcome(
                False,
                provenance.reason,
                candidate,
                "provenance",
                provenance=provenance,
            )

        solver_path = (self.solver_path or self.run_dir / "solve.py").resolve()
        hardcode = SolverStaticAnalyzer(solver_path).detect_hardcoded_candidate(candidate)
        if hardcode.hardcoded:
            return BlindVerificationOutcome(
                False,
                hardcode.reason,
                candidate,
                "hardcode",
                provenance=provenance,
                hardcode=hardcode,
            )

        policy = FlagPolicy.from_schema(self.flag_policy)
        if not policy.regex:
            return BlindVerificationOutcome(
                False,
                "flag policy regex is required for blind replay",
                candidate,
                "replay",
                provenance=provenance,
                hardcode=hardcode,
            )
        if not solver_path.is_file():
            return BlindVerificationOutcome(
                False,
                "solver file is missing",
                candidate,
                "replay",
                provenance=provenance,
                hardcode=hardcode,
            )

        with tempfile.TemporaryDirectory(prefix="ctf-blind-") as replay_dir:
            replay_root = Path(replay_dir)
            _copy_solver_and_preserved_artifacts(
                self.run_dir,
                solver_path,
                replay_root,
            )
            replay = replay_solver(
                replay_root / "solve.py",
                expected_flag=None,
                flag_regex=policy.regex,
                timeout_seconds=self.timeout_seconds,
            )
        if replay.matched_flag != candidate.normalized_value:
            return BlindVerificationOutcome(
                False,
                "blind replay did not emit the candidate selected by provenance",
                candidate,
                "replay",
                provenance=provenance,
                hardcode=hardcode,
                replay=replay,
            )

        with tempfile.TemporaryDirectory(prefix="ctf-blind-negative-") as negative_dir:
            negative_root = Path(negative_dir)
            shutil.copy2(solver_path, negative_root / "solve.py")
            negative_control = replay_solver(
                negative_root / "solve.py",
                expected_flag=None,
                flag_regex=policy.regex,
                timeout_seconds=self.timeout_seconds,
            )
        if negative_control.matched_flag == candidate.normalized_value:
            return BlindVerificationOutcome(
                False,
                "negative control emitted candidate without preserved source artifacts",
                candidate,
                "independent",
                provenance=provenance,
                hardcode=hardcode,
                replay=replay,
                negative_control=negative_control,
            )

        return BlindVerificationOutcome(
            True,
            "blind replay reproduced candidate and negative control removed it",
            candidate,
            provenance=provenance,
            hardcode=hardcode,
            replay=replay,
            negative_control=negative_control,
        )


def _copy_solver_and_preserved_artifacts(
    run_dir: Path,
    solver_path: Path,
    target: Path,
) -> None:
    shutil.copy2(solver_path, target / "solve.py")
    files_root = run_dir / "files"
    if files_root.is_dir():
        _copy_regular_tree(files_root, target / "files")


def _copy_regular_tree(source_root: Path, target_root: Path) -> None:
    for source in source_root.rglob("*"):
        if source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
