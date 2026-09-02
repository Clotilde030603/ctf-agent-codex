"""Typed contracts for persistent model-call budgeting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NewType, Protocol

from ctf_agent.lanes.model import ProvenancedFact

BudgetLeaseId = NewType("BudgetLeaseId", str)
BudgetRequestId = NewType("BudgetRequestId", str)


class BudgetRole(StrEnum):
    PLANNER = "planner"
    SOLVER = "solver"
    VERIFIER = "verifier"


class BudgetPurpose(StrEnum):
    PLAN = "plan"
    SOLVE = "solve"
    VERIFY = "verify"
    RETRY = "retry"
    REPLAN = "replan"
    RECOVERY = "recovery"


class LeaseStatus(StrEnum):
    RESERVED = "reserved"
    STARTED = "started"
    COMMITTED = "committed"
    RELEASED = "released"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    initial_limit: int
    hard_limit: int
    verifier_floor: int = 1
    planner_soft_limit: int = 1
    max_extensions: int = 0
    extension_size: int = 1
    retry_reserve: int = 0
    verifier_candidate_limit: int = 0

    def __post_init__(self) -> None:
        if self.initial_limit < 1:
            raise BudgetPolicyError("initial_limit", "must be positive")
        if self.hard_limit < self.initial_limit:
            raise BudgetPolicyError("hard_limit", "must not be below initial_limit")
        if not 0 <= self.verifier_floor <= self.initial_limit:
            raise BudgetPolicyError("verifier_floor", "must fit within initial_limit")
        if not 0 <= self.planner_soft_limit <= self.initial_limit:
            raise BudgetPolicyError("planner_soft_limit", "must fit within initial_limit")
        if self.max_extensions < 0:
            raise BudgetPolicyError("max_extensions", "must be non-negative")
        if self.extension_size < 1:
            raise BudgetPolicyError("extension_size", "must be positive")
        if self.retry_reserve < 0:
            raise BudgetPolicyError("retry_reserve", "must be non-negative")
        if self.verifier_candidate_limit < 0:
            raise BudgetPolicyError("verifier_candidate_limit", "must be non-negative")


@dataclass(frozen=True, slots=True)
class BudgetRequest:
    role: BudgetRole
    purpose: BudgetPurpose
    request_id: BudgetRequestId


@dataclass(frozen=True, slots=True)
class BudgetLease:
    lease_id: BudgetLeaseId
    request_id: BudgetRequestId
    role: BudgetRole
    purpose: BudgetPurpose
    status: LeaseStatus
    borrowed: bool


class ModelBudgetLeaser(Protocol):
    async def acquire(self, request: BudgetRequest) -> BudgetLease: ...

    async def start(self, lease_id: BudgetLeaseId) -> BudgetLease: ...

    async def commit(self, lease_id: BudgetLeaseId) -> BudgetLease: ...

    async def release(self, lease_id: BudgetLeaseId) -> BudgetLease: ...


@dataclass(frozen=True, slots=True)
class BudgetRoleTotals:
    role: BudgetRole
    requested: int
    used: int
    reserved: int
    borrowed: int
    extended: int

    def to_dict(self) -> dict[str, int]:
        return {
            "requested": self.requested,
            "used": self.used,
            "reserved": self.reserved,
            "borrowed": self.borrowed,
            "extended": self.extended,
        }


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    run_id: str
    initial_limit: int
    active_limit: int
    hard_limit: int
    requested: int
    used: int
    reserved: int
    reserved_unused: int
    borrowed: int
    extended: int
    extensions: int
    max_extensions: int
    leases: tuple[BudgetLease, ...]
    roles: tuple[BudgetRoleTotals, ...]
    final_stop_reason: str

    def to_dict(
        self,
    ) -> dict[
        str,
        int | str | list[dict[str, str | bool]] | dict[str, dict[str, int]],
    ]:
        return {
            "run_id": self.run_id,
            "initial_limit": self.initial_limit,
            "active_limit": self.active_limit,
            "hard_limit": self.hard_limit,
            "requested": self.requested,
            "used": self.used,
            "reserved": self.reserved,
            "reserved_unused": self.reserved_unused,
            "borrowed": self.borrowed,
            "extended": self.extended,
            "extensions": self.extensions,
            "max_extensions": self.max_extensions,
            "roles": {totals.role.value: totals.to_dict() for totals in self.roles},
            "final_stop_reason": self.final_stop_reason,
            "leases": [
                {
                    "lease_id": lease.lease_id,
                    "request_id": lease.request_id,
                    "role": lease.role.value,
                    "purpose": lease.purpose.value,
                    "status": lease.status.value,
                    "borrowed": lease.borrowed,
                }
                for lease in self.leases
            ],
        }


@dataclass(frozen=True, slots=True)
class BudgetExhaustedError(RuntimeError):
    role: BudgetRole
    purpose: BudgetPurpose
    request_id: BudgetRequestId
    reason: str

    def __str__(self) -> str:
        return f"model budget exhausted for {self.role.value}/{self.purpose.value}: {self.reason}"


@dataclass(frozen=True, slots=True)
class ArtifactProgress:
    """Controller-observed artifact plus its content-addressed identity."""

    path: Path
    content_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateReceipt:
    """Controller-issued one-way receipt for an observed candidate."""

    candidate_sha256: str


@dataclass(frozen=True, slots=True)
class ProgressEvidence:
    """Controller-owned proof that a bounded extension is warranted."""

    facts: tuple[ProvenancedFact, ...] = ()
    artifacts: tuple[ArtifactProgress, ...] = ()
    candidates: tuple[CandidateReceipt, ...] = ()
    role: BudgetRole = BudgetRole.SOLVER


@dataclass(frozen=True, slots=True)
class BudgetPolicyError(ValueError):
    field: str
    reason: str

    def __str__(self) -> str:
        return f"invalid budget policy {self.field}: {self.reason}"


@dataclass(frozen=True, slots=True)
class BudgetNotFoundError(KeyError):
    resource: str

    def __str__(self) -> str:
        return f"model budget resource not found: {self.resource}"


@dataclass(frozen=True, slots=True)
class BudgetPersistenceError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return f"invalid persisted model budget state: {self.reason}"


@dataclass(frozen=True, slots=True)
class BudgetLeaseStateError(RuntimeError):
    lease_id: BudgetLeaseId
    expected: LeaseStatus
    actual: LeaseStatus

    def __str__(self) -> str:
        return (
            f"budget lease {self.lease_id} expected {self.expected.value}, "
            f"found {self.actual.value}"
        )


@dataclass(frozen=True, slots=True)
class BudgetDatabase:
    path: Path
