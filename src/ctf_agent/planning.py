"""Hypothesis parsing, model planning, and semantic deduplication."""

from __future__ import annotations

import json
from typing import Any, Final, Protocol

from ctf_agent.frontier import semantic_hypothesis_key
from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelRequest
from ctf_agent.schemas import Hypothesis
from ctf_agent.skills import SkillSelection

MAX_ACTIVE_HYPOTHESES: Final = 3
MAX_HYPOTHESIS_POOL: Final = 12

HYPOTHESIS_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_HYPOTHESIS_POOL,
            "items": Hypothesis.model_json_schema(),
        }
    },
}


class HypothesisPlanner(Protocol):
    async def plan(self, context: dict[str, object]) -> tuple[Hypothesis, ...]: ...


class ModelHypothesisPlanner:
    def __init__(
        self,
        backend: ModelBackend,
        max_hypotheses: int = MAX_ACTIVE_HYPOTHESES,
        skills: SkillSelection | None = None,
        role: str = "planner",
    ) -> None:
        if not 1 <= max_hypotheses <= MAX_HYPOTHESIS_POOL:
            msg = f"max_hypotheses must be between 1 and {MAX_HYPOTHESIS_POOL}"
            raise ValueError(msg)
        self._backend = backend
        self._max_hypotheses = max_hypotheses
        self._skills = skills
        self._role = role
        self.last_projection_manifest: dict[str, Any] | None = None

    async def plan(self, context: dict[str, object]) -> tuple[Hypothesis, ...]:
        response = await self._backend.complete(
            ModelRequest(
                system="Return only JSON for independent CTF solving hypotheses.",
                prompt=(
                    f"Create up to {self._max_hypotheses} independent CTF solving hypotheses. "
                    "Respond as a JSON object with a 'hypotheses' array. Each item needs "
                    "id, claim, expected_signal, cost, confidence, kill_condition, and "
                    "success_condition."
                ),
                context=context,
                output_schema=HYPOTHESIS_RESPONSE_SCHEMA,
                role=self._role,
                developer=(
                    self._skills.developer_instructions if self._skills is not None else None
                ),
                skill_runtime=self._skills.runtime if self._skills is not None else None,
            )
        )
        manifest = response.metadata.get("projection_manifest")
        self.last_projection_manifest = manifest if isinstance(manifest, dict) else None
        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ModelBackendError("planner response content must be JSON") from exc
        raw_hypotheses = payload.get("hypotheses") if isinstance(payload, dict) else None
        if not isinstance(raw_hypotheses, list):
            raise ModelBackendError("planner JSON must include a hypotheses list")
        try:
            hypotheses = [
                Hypothesis.model_validate(item)
                for item in raw_hypotheses[: self._max_hypotheses]
            ]
        except (TypeError, ValueError) as exc:
            raise ModelBackendError("planner hypotheses failed schema validation") from exc
        unique = deduplicate_hypotheses(hypotheses)
        if not unique:
            raise ModelBackendError("planner produced no valid hypotheses")
        return tuple(unique)


class StaticHypothesisPlanner:
    def __init__(self, hypotheses: list[Hypothesis]) -> None:
        self._hypotheses = tuple(deduplicate_hypotheses(hypotheses))

    async def plan(self, _context: dict[str, object]) -> tuple[Hypothesis, ...]:
        return self._hypotheses


def hypothesis_from_mapping(index: int, value: dict[str, Any]) -> Hypothesis:
    evidence = value.get("supporting_evidence")
    if not isinstance(evidence, list):
        rationale = value.get("rationale")
        evidence = [str(rationale)] if rationale else []
    confidence = value.get("confidence", 0.5)
    if not isinstance(confidence, int | float):
        confidence = 0.5
    cost = value.get("cost", "medium")
    if cost not in {"low", "medium", "high"}:
        cost = "medium"
    return Hypothesis(
        id=str(value.get("id") or f"h{index + 1}"),
        claim=str(value.get("claim") or value.get("title") or f"Hypothesis {index + 1}"),
        supporting_evidence=[str(item) for item in evidence],
        expected_signal=str(
            value.get("expected_signal") or value.get("strategy") or "observable progress"
        ),
        cost=cost,
        confidence=float(confidence),
        required_tools=[str(item) for item in value.get("required_tools", []) if item],
        kill_condition=str(
            value.get("kill_condition") or "no supporting signal after one lane run"
        ),
        success_condition=str(
            value.get("success_condition") or "validated flag candidate is produced"
        ),
    )


def deduplicate_hypotheses(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    unique: list[Hypothesis] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for hypothesis in hypotheses:
        fingerprint = semantic_hypothesis_key(hypothesis)
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(hypothesis)
    return unique
