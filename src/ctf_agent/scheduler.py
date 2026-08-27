from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from ctf_agent.models.base import ModelBackend, ModelBackendError, ModelRequest
from ctf_agent.schemas import Hypothesis, SpecialistResult
from ctf_agent.specialists.base import Specialist, progress_made

MAX_HYPOTHESES = 3

HYPOTHESIS_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_HYPOTHESES,
            "items": Hypothesis.model_json_schema(),
        }
    },
}


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


@dataclass(frozen=True)
class SchedulerRunResult:
    hypotheses: tuple[Hypothesis, ...]
    specialist_results: tuple[SpecialistResult, ...]
    solved: bool
    stop_reason: str

    @property
    def accepted_flags(self) -> tuple[str, ...]:
        flags: list[str] = []
        for result in self.specialist_results:
            if result.status == "confirmed":
                flags.extend(candidate.value for candidate in result.flag_candidates)
        return tuple(dict.fromkeys(flags))


class HypothesisPlanner(Protocol):
    async def plan(self, context: dict[str, object]) -> tuple[Hypothesis, ...]:
        """Return up to MAX_HYPOTHESES independent solving hypotheses."""
        ...


class ModelHypothesisPlanner:
    def __init__(self, backend: ModelBackend, max_hypotheses: int = MAX_HYPOTHESES) -> None:
        if max_hypotheses < 1 or max_hypotheses > MAX_HYPOTHESES:
            raise ValueError("max_hypotheses must be between 1 and 3")
        self._backend = backend
        self._max_hypotheses = max_hypotheses

    async def plan(self, context: dict[str, object]) -> tuple[Hypothesis, ...]:
        response = await self._backend.complete(
            ModelRequest(
                system="Return only JSON for independent CTF solving hypotheses.",
                prompt=(
                    "Create up to three independent CTF solving hypotheses. "
                    "Respond as a JSON object with a 'hypotheses' array. "
                    "Each item needs id, claim, expected_signal, cost, confidence, "
                    "kill_condition, and success_condition."
                ),
                context=context,
                output_schema=HYPOTHESIS_RESPONSE_SCHEMA,
                role="planner",
            )
        )
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
        if not hypotheses:
            raise ModelBackendError("planner produced no valid hypotheses")
        return tuple(hypotheses)


class StaticHypothesisPlanner:
    def __init__(self, hypotheses: list[Hypothesis]) -> None:
        self._hypotheses = tuple(hypotheses[:MAX_HYPOTHESES])

    async def plan(self, context: dict[str, object]) -> tuple[Hypothesis, ...]:
        return self._hypotheses


@dataclass
class Scheduler:
    planner: HypothesisPlanner
    specialists: tuple[Specialist, ...]
    no_progress_cutoff: int = 1
    max_rounds: int = 1
    max_concurrency: int = MAX_HYPOTHESES

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or self.max_concurrency > MAX_HYPOTHESES:
            raise ValueError("max_concurrency must be between 1 and 3")

    async def run(self, context: dict[str, object]) -> SchedulerRunResult:
        hypotheses = await self.planner.plan(context)
        if not self.specialists:
            return SchedulerRunResult(hypotheses, (), False, "no_specialists")

        consecutive_no_progress = 0
        stop_reason = "max_rounds"
        results: list[SpecialistResult] = []

        for _round in range(self.max_rounds):
            round_results = await self._run_round(hypotheses, context)
            results.extend(round_results)

            if any(
                result.status == "confirmed" and result.flag_candidates
                for result in round_results
            ):
                stop_reason = "solved"
                break

            if any(progress_made(result) for result in round_results):
                consecutive_no_progress = 0
            else:
                consecutive_no_progress += 1
                if consecutive_no_progress >= self.no_progress_cutoff:
                    stop_reason = "no_progress"
                    break

        return SchedulerRunResult(
            hypotheses=hypotheses,
            specialist_results=tuple(results),
            solved=stop_reason == "solved",
            stop_reason=stop_reason,
        )

    async def _run_round(
        self,
        hypotheses: tuple[Hypothesis, ...],
        context: dict[str, object],
    ) -> list[SpecialistResult]:
        tasks: list[asyncio.Task[SpecialistResult]] = []
        semaphore = asyncio.Semaphore(self.max_concurrency)
        for hypothesis in hypotheses:
            matching = [
                specialist
                for specialist in self.specialists
                if specialist.supports(hypothesis.claim)
            ]
            selected = matching or list(self.specialists)
            for specialist in selected:
                tasks.append(
                    asyncio.create_task(
                        self._solve_lane_limited(
                            semaphore, specialist, hypothesis, context
                        )
                    )
                )

        results: list[SpecialistResult] = []
        for task in asyncio.as_completed(tasks):
            results.append(await task)
        return results

    @classmethod
    async def _solve_lane_limited(
        cls,
        semaphore: asyncio.Semaphore,
        specialist: Specialist,
        hypothesis: Hypothesis,
        context: dict[str, object],
    ) -> SpecialistResult:
        async with semaphore:
            return await cls._solve_lane(specialist, hypothesis, context)

    @staticmethod
    async def _solve_lane(
        specialist: Specialist,
        hypothesis: Hypothesis,
        context: dict[str, object],
    ) -> SpecialistResult:
        try:
            return await specialist.solve(hypothesis, context)
        except Exception as exc:
            return SpecialistResult(
                hypothesis_id=hypothesis.id,
                status="inconclusive",
                next_action=(
                    f"{specialist.name} failed with {type(exc).__name__}: {exc}"
                ),
                confidence=0.0,
            )
