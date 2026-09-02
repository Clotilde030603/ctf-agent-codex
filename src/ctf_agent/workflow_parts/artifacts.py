"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

import shutil
from pathlib import Path

from ctf_agent.reproduction import controller_reproduction_spec
from ctf_agent.schemas import (
    SpecialistResult,
)


def _promote_solver(
    run_dir: Path, results: tuple[SpecialistResult, ...]
) -> tuple[SpecialistResult, ...]:
    for result_index, result in enumerate(results):
        if not result.flag_candidates:
            continue
        for artifact in result.artifacts:
            candidate_path = (run_dir / artifact).resolve()
            if (
                candidate_path.name != "solve.py"
                or not candidate_path.is_file()
                or run_dir not in candidate_path.parents
            ):
                continue
            shutil.copy2(candidate_path, run_dir / "solve.py")
            requirements = candidate_path.parent / "requirements.txt"
            if requirements.is_file():
                shutil.copy2(requirements, run_dir / "requirements.txt")
            promoted_candidates = [
                candidate.model_copy(
                    update={
                        "reproduction_spec": controller_reproduction_spec(
                            run_dir,
                            run_dir,
                            candidate.reproduction_spec.argv,
                            requires_auth_handle=(candidate.reproduction_spec.requires_auth_handle),
                        )
                    }
                )
                if candidate.reproduction_spec is not None
                else candidate
                for candidate in result.flag_candidates
            ]
            promoted_result = result.model_copy(update={"flag_candidates": promoted_candidates})
            return (
                *results[:result_index],
                promoted_result,
                *results[result_index + 1 :],
            )
    return results
