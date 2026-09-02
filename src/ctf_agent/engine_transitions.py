"""Deterministic controller execution and state advancement."""

import asyncio
from typing import assert_never

from ctf_agent.config import Settings
from ctf_agent.engine_types import RunContext, StateHandler, StateOutcome
from ctf_agent.schemas import RunRecord, RunState
from ctf_agent.state_transitions import InvalidTransition

_TERMINAL_STATES = {
    RunState.DONE,
    RunState.DONE_WITH_WARNINGS,
    RunState.READY,
    RunState.FAILED,
    RunState.NEEDS_AUTHENTICATION,
}
_RECOVERABLE_EVIDENCE_STATES = {
    RunState.EVIDENCE_PENDING,
    RunState.WRITEUP_PENDING,
    RunState.EVIDENCE,
    RunState.WRITEUP,
}


def _route_resumed_authentication(context: RunContext) -> None:
    if context.values.pop("resumed", False) is not True:
        return
    state = context.record.state
    target = context.store.reauthentication_target(context.record.run_id)
    match state:
        case RunState.SOLVE | RunState.VERIFY:
            target = state
            context.record = context.store.begin_reauthentication(
                context.record.run_id, target
            )
        case RunState.NEEDS_AUTHENTICATION:
            if target is None:
                raise InvalidTransition(
                    "authentication user-action state has no resume intent"
                )
            context.record = context.store.begin_reauthentication(
                context.record.run_id, target
            )
        case RunState.AUTHENTICATE:
            return
        case (
            RunState.INGEST
            | RunState.TRIAGE
            | RunState.PLAN
            | RunState.SUBMIT
            | RunState.EVIDENCE_PENDING
            | RunState.WRITEUP_PENDING
            | RunState.EVIDENCE
            | RunState.WRITEUP
            | RunState.REPRODUCE
            | RunState.READY
            | RunState.DONE
            | RunState.DONE_WITH_WARNINGS
            | RunState.FAILED
        ):
            return
        case unreachable:
            assert_never(unreachable)
    if target is None:
        raise InvalidTransition("authentication route has no return state")
    context.ledger.append(
        context.record.run_id,
        "authentication.required",
        {"return_state": target.value, "reason": "process_restart"},
        state=RunState.AUTHENTICATE.value,
    )
    context.ledger.append(
        context.record.run_id,
        "state.transition",
        {"from": state.value, "to": RunState.AUTHENTICATE.value},
        state=RunState.AUTHENTICATE.value,
    )


async def execute_transitions(
    context: RunContext,
    handlers: dict[RunState, StateHandler],
    settings: Settings,
) -> RunRecord:
    _route_resumed_authentication(context)
    steps = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.total_run_timeout_seconds
    while context.record.state not in _TERMINAL_STATES:
        steps += 1
        if steps > settings.max_state_steps:
            context.record = context.store.transition(
                context.record.run_id,
                RunState.FAILED,
                "maximum deterministic state-step budget exhausted",
            )
            return context.record
        state = context.record.state
        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            context.record = context.store.transition(
                context.record.run_id,
                RunState.FAILED,
                "total run timeout exhausted",
            )
            return context.record
        handler = handlers.get(state)
        if handler is None:
            raise RuntimeError(f"no handler registered for {state}")
        task_key = f"state:{state.value}"
        context.ledger.append(
            context.record.run_id,
            "state.started",
            {},
            state=state.value,
        )
        try:
            handler_task: asyncio.Future[StateOutcome] = asyncio.ensure_future(
                handler(context)
            )
            done, _pending = await asyncio.wait(
                {handler_task}, timeout=remaining_seconds
            )
            if not done:
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
                context.ledger.append(
                    context.record.run_id,
                    "run.timeout",
                    {
                        "state": state.value,
                        "timeout_seconds": settings.total_run_timeout_seconds,
                    },
                    state=state.value,
                )
                context.record = context.store.transition(
                    context.record.run_id,
                    RunState.FAILED,
                    "total run timeout exhausted",
                )
                return context.record
            outcome = handler_task.result()
        except asyncio.CancelledError:
            context.ledger.append(
                context.record.run_id,
                "run.interrupted",
                {"checkpoint": state.value},
                state=state.value,
            )
            raise
        except Exception as exc:
            context.ledger.append(
                context.record.run_id,
                "state.error",
                {"error_type": type(exc).__name__, "message": str(exc)},
                state=state.value,
            )
            if state in _RECOVERABLE_EVIDENCE_STATES and context.store.has_accepted_submission(
                context.record.run_id
            ):
                context.record = context.store.record_recoverable_error(
                    context.record.run_id, str(exc)
                )
            else:
                context.record = context.store.transition(
                    context.record.run_id, RunState.FAILED, str(exc)
                )
            return context.record
        context.record = context.store.complete_state(
            context.record.run_id,
            expected_state=state,
            target=outcome.target,
            task_key=task_key,
            error=outcome.error,
        )
        context.ledger.append(
            context.record.run_id,
            "state.completed",
            outcome.payload,
            state=state.value,
            idempotency_key=task_key,
        )
        context.ledger.append(
            context.record.run_id,
            "state.transition",
            {"from": state.value, "to": outcome.target.value},
            state=outcome.target.value,
        )
    return context.record
