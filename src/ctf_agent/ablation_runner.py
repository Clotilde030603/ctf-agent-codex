"""Deterministic paired B0-B5 benchmark execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import anyio
import yaml

from ctf_agent.ablation_report import build_ablation_report
from ctf_agent.ablation_schema import (
    AblationCondition,
    AblationMatrix,
    InvalidEvaluationMetadata,
    PairedRunIdentity,
)
from ctf_agent.benchmark import (
    BenchmarkChallenge,
    BenchmarkManifest,
    BenchmarkRunRecord,
    _load_manifest,
    _run_once,
)

RunPair = tuple[PairedRunIdentity, BenchmarkRunRecord]


def _load_matrix(path: Path) -> AblationMatrix:
    return AblationMatrix.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    )


def _required_identity(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise InvalidEvaluationMetadata(f"manifest missing {field}")
    return value


def _validate_bindings(
    manifest_path: Path,
    manifest: BenchmarkManifest,
    matrix: AblationMatrix,
) -> None:
    identities = {
        "evaluation_id": _required_identity(manifest.evaluation_id, "evaluation_id"),
        "dataset_revision": _required_identity(
            manifest.dataset_revision, "dataset_revision"
        ),
        "ablation_revision": _required_identity(
            manifest.ablation_revision, "ablation_revision"
        ),
    }
    for field, expected in identities.items():
        actual = getattr(matrix, field)
        if actual != expected:
            raise InvalidEvaluationMetadata(
                f"{field} mismatch: manifest={expected}, matrix={actual}"
            )
    if not 12 <= len(manifest.challenges) <= 20:
        raise InvalidEvaluationMetadata("evaluation must contain 12-20 challenges")
    root = manifest_path.parent.resolve()
    for challenge in manifest.challenges:
        _validate_case(root, challenge)


def _validate_case(root: Path, challenge: BenchmarkChallenge) -> None:
    if challenge.case_id != challenge.id:
        raise InvalidEvaluationMetadata(f"case_id mismatch for {challenge.id}")
    if challenge.fixture_sha256 is None or len(challenge.artifact_paths) != 1:
        raise InvalidEvaluationMetadata(f"{challenge.id} missing fixture_sha256 identity")
    fixture = (root / challenge.artifact_paths[0]).resolve()
    try:
        fixture.relative_to(root)
    except ValueError as exc:
        raise InvalidEvaluationMetadata(
            f"{challenge.id} fixture path escapes dataset"
        ) from exc
    actual_fixture = (
        hashlib.sha256(fixture.read_bytes()).hexdigest()
        if fixture.is_file()
        else "missing"
    )
    if actual_fixture != challenge.fixture_sha256:
        raise InvalidEvaluationMetadata(
            f"{challenge.id} fixture_sha256 mismatch: "
            f"expected {challenge.fixture_sha256}, got {actual_fixture}"
        )
    if challenge.solution_path is None or challenge.solution_sha256 is None:
        raise InvalidEvaluationMetadata(f"{challenge.id} missing solution identity")
    solution = (root / challenge.solution_path).resolve()
    try:
        solution.relative_to(root)
    except ValueError as exc:
        raise InvalidEvaluationMetadata(
            f"{challenge.id} solution path escapes dataset"
        ) from exc
    actual_solution = (
        hashlib.sha256(solution.read_bytes()).hexdigest()
        if solution.is_file()
        else "missing"
    )
    if actual_solution != challenge.solution_sha256:
        raise InvalidEvaluationMetadata(
            f"{challenge.id} solution_sha256 mismatch: "
            f"expected {challenge.solution_sha256}, got {actual_solution}"
        )


async def run_ablation_benchmark(
    manifest_path: Path,
    matrix_path: Path,
    solve_k: int = 3,
) -> dict[str, Any]:
    """Execute every case/condition/repeat pair and return a stable scorer report."""
    if solve_k < 1:
        raise InvalidEvaluationMetadata("solve_k must be at least one")
    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    matrix = _load_matrix(matrix_path.resolve())
    _validate_bindings(manifest_path, manifest, matrix)
    runs: list[RunPair] = []
    for condition in matrix.conditions:
        for challenge in manifest.challenges:
            repeat_runs = challenge.repeat_runs or manifest.repeat_runs
            for repeat_index in range(1, repeat_runs + 1):
                record = await _run_once(
                    manifest_path,
                    challenge,
                    repeat_index,
                    timeout_seconds=challenge.timeout_seconds
                    or manifest.timeout_seconds,
                    condition=condition,
                )
                runs.append(
                    (
                        _run_identity(
                            manifest, challenge, condition, repeat_index
                        ),
                        record,
                    )
                )
    _validate_pairs(manifest, matrix, runs)
    return build_ablation_report(manifest, matrix, runs, solve_k)


def _run_identity(
    manifest: BenchmarkManifest,
    challenge: BenchmarkChallenge,
    condition: AblationCondition,
    repeat_index: int,
) -> PairedRunIdentity:
    return PairedRunIdentity(
        evaluation_id=_required_identity(manifest.evaluation_id, "evaluation_id"),
        dataset_revision=_required_identity(
            manifest.dataset_revision, "dataset_revision"
        ),
        ablation_revision=_required_identity(
            manifest.ablation_revision, "ablation_revision"
        ),
        case_id=challenge.case_id or challenge.id,
        condition_id=condition.condition_id,
        repeat_index=repeat_index,
        model_id=condition.model_id,
        reasoning_id=condition.reasoning_id,
        tool_image_digest=condition.tool_image_digest,
        capability_snapshot_digest=condition.capability_snapshot_digest,
        skill_ids=condition.skill_ids,
        solver_id=condition.solver_id,
        artifact_id=condition.artifact_id,
        fixture_sha256=challenge.fixture_sha256 or "",
        solution_sha256=challenge.solution_sha256 or "",
        config_sha256=condition.config_sha256,
        seed=condition.seed,
    )


def _validate_pairs(
    manifest: BenchmarkManifest,
    matrix: AblationMatrix,
    runs: list[RunPair],
) -> None:
    actual = [
        (identity.case_id, identity.condition_id, identity.repeat_index)
        for identity, _ in runs
    ]
    expected = [
        (challenge.case_id or challenge.id, condition.condition_id, repeat_index)
        for condition in matrix.conditions
        for challenge in manifest.challenges
        for repeat_index in range(
            1, (challenge.repeat_runs or manifest.repeat_runs) + 1
        )
    ]
    if len(set(actual)) != len(actual):
        raise InvalidEvaluationMetadata("duplicate case/condition/repeat identity")
    if actual != expected:
        raise InvalidEvaluationMetadata("incomplete case/condition/repeat pairs")
    conditions = {item.condition_id: item for item in matrix.conditions}
    for identity, record in runs:
        observed = record.observed_runtime_identity
        if observed is None:
            raise InvalidEvaluationMetadata("autonomous run lacks observed runtime identity")
        condition = conditions[identity.condition_id]
        if observed.capability_snapshot_digest != condition.capability_snapshot_digest:
            raise InvalidEvaluationMetadata(
                "observed capability snapshot digest mismatches paired condition"
            )
        if observed.config_sha256 != condition.config_sha256:
            raise InvalidEvaluationMetadata("observed runtime identity mismatches paired condition")


def ablation_benchmark(
    manifest: Path,
    matrix: Path,
    *,
    solve_k: int = 3,
) -> dict[str, Any]:
    """Synchronous CLI boundary for paired evaluation."""
    return anyio.run(run_ablation_benchmark, manifest, matrix, solve_k)
