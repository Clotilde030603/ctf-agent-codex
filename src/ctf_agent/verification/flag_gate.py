"""Deterministic gate for deciding whether a flag may be submitted."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from re import Pattern
from typing import Any

from .candidate import FlagCandidate, normalize_flag

_PLACEHOLDER_WORDS = frozenset(
    {
        "changeme",
        "dummy",
        "example",
        "fake",
        "placeholder",
        "redacted",
        "sample",
        "test",
        "todo",
        "your_flag",
    }
)


@dataclass(frozen=True, slots=True)
class FlagPolicy:
    regex: str | None = None
    prefix: str | None = None
    min_length: int = 6
    max_length: int = 256
    examples: tuple[str, ...] = ()
    case_sensitive: bool = True

    @classmethod
    def from_schema(cls, value: Any) -> FlagPolicy:
        if isinstance(value, cls):
            return value
        data = _as_mapping(value)
        return cls(
            regex=data.get("regex") or data.get("pattern"),
            prefix=data.get("prefix"),
            min_length=int(data.get("min_length") or 6),
            max_length=int(data.get("max_length") or 256),
            examples=tuple(str(item) for item in (data.get("examples") or ())),
            case_sensitive=bool(data.get("case_sensitive", True)),
        )

    def compiled_regex(self) -> Pattern[str] | None:
        if not self.regex:
            return None
        return re.compile(self.regex, flags=0 if self.case_sensitive else re.IGNORECASE)


@dataclass(slots=True)
class SubmissionBudget:
    max_attempts: int
    used_attempts: int = 0

    def remaining(self) -> int:
        return max(self.max_attempts - self.used_attempts, 0)

    def can_submit(self) -> bool:
        return self.remaining() > 0

    def reserve(self) -> bool:
        if not self.can_submit():
            return False
        self.used_attempts += 1
        return True


@dataclass(slots=True)
class RejectedCandidates:
    _values: set[str] = field(default_factory=set)

    def add(self, value: str) -> None:
        self._values.add(normalize_flag(value))

    def update(self, values: Iterable[str]) -> None:
        for value in values:
            self.add(value)

    def contains(self, value: str) -> bool:
        return normalize_flag(value) in self._values


@dataclass(frozen=True, slots=True)
class FlagGateDecision:
    allowed: bool
    reason: str
    candidate: FlagCandidate


@dataclass(slots=True)
class FlagGate:
    policy: FlagPolicy | object
    rejected_candidates: RejectedCandidates = field(default_factory=RejectedCandidates)

    def __post_init__(self) -> None:
        self.policy = FlagPolicy.from_schema(self.policy)

    def evaluate(
        self,
        candidate_value: object,
        budget: SubmissionBudget | None = None,
        *,
        require_provenance: bool = True,
    ) -> FlagGateDecision:
        policy = FlagPolicy.from_schema(self.policy)
        candidate = FlagCandidate.from_schema(candidate_value)
        value = candidate.normalized_value

        if candidate.rejected:
            return self._reject(candidate, candidate.reject_reason or "candidate already rejected")
        if self.rejected_candidates.contains(value):
            return self._reject(candidate, "candidate was rejected previously")
        if budget is not None and not budget.can_submit():
            return self._reject(candidate, "submission budget exhausted")
        if not value:
            return self._reject(candidate, "empty flag candidate")
        if len(value) < policy.min_length:
            return self._reject(candidate, "flag candidate is too short")
        if len(value) > policy.max_length:
            return self._reject(candidate, "flag candidate is too long")
        if policy.prefix and not _starts_with(value, policy.prefix, policy.case_sensitive):
            return self._reject(candidate, "flag candidate does not match required prefix")
        regex = policy.compiled_regex()
        if regex and not regex.fullmatch(value):
            return self._reject(candidate, "flag candidate does not match required format")
        if value in policy.examples:
            return self._reject(candidate, "flag candidate matches a documented example")
        if _looks_like_placeholder(value):
            return self._reject(candidate, "flag candidate looks like a placeholder or sample")
        if require_provenance and not candidate.has_actionable_provenance():
            return self._reject(candidate, "flag candidate lacks actionable provenance")

        return FlagGateDecision(True, "accepted by deterministic gate", candidate)

    def reserve_submission(self, decision: FlagGateDecision, budget: SubmissionBudget) -> bool:
        if not decision.allowed:
            return False
        return budget.reserve()

    @staticmethod
    def _reject(candidate: FlagCandidate, reason: str) -> FlagGateDecision:
        return FlagGateDecision(False, reason, candidate)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    body_match = re.search(r"\{(?P<body>.*)\}", lowered)
    tokens = {lowered}
    if body_match:
        body = body_match.group("body")
        tokens.add(body)
        tokens.update(part for part in re.split(r"[^a-z0-9_]+", body) if part)
    if lowered in {"flag", "ctf", "flag{}", "ctf{}"}:
        return True
    return any(token in _PLACEHOLDER_WORDS for token in tokens)


def _starts_with(value: str, prefix: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return value.startswith(prefix)
    return value.lower().startswith(prefix.lower())


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    if hasattr(value, "dict"):
        dumped = value.dict()
        if isinstance(dumped, Mapping):
            return dumped
    names = ("regex", "pattern", "prefix", "min_length", "max_length", "examples", "case_sensitive")
    return {name: getattr(value, name) for name in names if hasattr(value, name)}
