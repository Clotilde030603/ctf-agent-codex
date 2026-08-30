"""Blind model reviewer that re-derives flags without receiving the solver candidate."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ctf_agent.config import Settings
from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelRequest
from ctf_agent.models.factory import create_codex_backend

ReviewerBackendFactory = Callable[[Settings, str, Path], ModelBackend]


class ModelReviewResponse(BaseModel):
    derived_candidates: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    reproduction_command: str = "python3 solve.py"


@dataclass(frozen=True, slots=True)
class ModelReviewOutcome:
    accepted: bool
    reason: str
    derived_candidates: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()


@dataclass(slots=True)
class ModelBlindReviewer:
    settings: Settings
    run_dir: Path
    flag_policy: dict[str, object]
    backend_factory: ReviewerBackendFactory = create_codex_backend

    async def derive(self) -> ModelReviewOutcome:
        solver = self.run_dir / "solve.py"
        if not solver.is_file():
            return ModelReviewOutcome(False, "reviewer solver file is missing")
        with tempfile.TemporaryDirectory(prefix="ctf-model-review-") as temp_dir:
            review_dir = Path(temp_dir)
            shutil.copy2(solver, review_dir / "solve.py")
            files = self.run_dir / "files"
            if files.is_dir():
                _copy_regular_tree(files, review_dir / "files")
            inventory = _file_inventory(review_dir / "files")
            backend = self.backend_factory(self.settings, "verifier", review_dir)
            try:
                response = await backend.complete(
                    ModelRequest(
                        role="verifier",
                        system=(
                            "Blindly verify an authorized CTF solver. The expected candidate is "
                            "not provided. Inspect original files and solve.py, run the solver if "
                            "needed, and return only independently derived candidates."
                        ),
                        prompt=(
                            "Derive every flag matching the supplied policy from files/ using "
                            "solve.py. Do not infer an expected value from metadata."
                        ),
                        context={
                            "flag_policy": self.flag_policy,
                            "solver": "solve.py",
                            "files": inventory,
                        },
                        output_schema=ModelReviewResponse.model_json_schema(),
                    )
                )
            except ModelBackendError as exc:
                return ModelReviewOutcome(
                    False,
                    f"reviewer backend failed: {type(exc).__name__}: {exc}",
                )
        try:
            payload = ModelReviewResponse.model_validate(json.loads(response.content))
        except (json.JSONDecodeError, ValueError) as exc:
            return ModelReviewOutcome(False, f"reviewer response invalid: {exc}")
        candidates = tuple(dict.fromkeys(payload.derived_candidates))
        if not candidates:
            return ModelReviewOutcome(
                False,
                "reviewer independently derived no candidates",
                facts=tuple(payload.facts),
            )
        return ModelReviewOutcome(
            True,
            "reviewer independently derived candidate set",
            candidates,
            tuple(payload.facts),
        )


def _copy_regular_tree(source_root: Path, target_root: Path) -> None:
    for source in source_root.rglob("*"):
        if source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _file_inventory(root: Path) -> list[dict[str, object]]:
    if not root.is_dir():
        return []
    output: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        output.append(
            {
                "path": path.relative_to(root.parent).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest,
            }
        )
    return output
