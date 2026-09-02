"""Extracted workflow behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctf_agent.workflow import AutonomousWorkflow


from ctf_agent.budget import ModelBudgetBroker
from ctf_agent.engine import RunContext


def _model_budget(workflow: AutonomousWorkflow, context: RunContext) -> ModelBudgetBroker:
    broker = context.values.get("model_budget")
    if not isinstance(broker, ModelBudgetBroker):
        raise RuntimeError("run context has no persistent model budget")

    def observe(event_type: str, payload: dict[str, str | int | bool]) -> None:
        context.ledger.append(
            context.record.run_id,
            event_type,
            payload | workflow._budget_report(context),
            state=context.record.state.value,
        )

    broker.observe(observe)
    return broker


def _budget_report(
    workflow: AutonomousWorkflow, context: RunContext
) -> dict[str, int | str | dict[str, dict[str, int]]]:
    broker = context.values.get("model_budget")
    if not isinstance(broker, ModelBudgetBroker):
        return {
            "requested": 0,
            "used": 0,
            "reserved": 0,
            "borrowed": 0,
            "extended": 0,
            "roles": {
                role: {
                    "requested": 0,
                    "used": 0,
                    "reserved": 0,
                    "borrowed": 0,
                    "extended": 0,
                }
                for role in ("planner", "solver", "verifier")
            },
            "final_stop_reason": "",
        }
    snapshot = broker.snapshot()
    return {
        "requested": snapshot.requested,
        "used": snapshot.used,
        "reserved": snapshot.reserved,
        "borrowed": snapshot.borrowed,
        "extended": snapshot.extended,
        "roles": {totals.role.value: totals.to_dict() for totals in snapshot.roles},
        "final_stop_reason": snapshot.final_stop_reason,
    }
