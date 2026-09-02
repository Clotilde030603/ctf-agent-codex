"""Workflow state-transition rules."""

from ctf_agent.schemas import RunState

FORWARD_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.AUTHENTICATE: {
        RunState.INGEST,
        RunState.SOLVE,
        RunState.VERIFY,
        RunState.NEEDS_AUTHENTICATION,
        RunState.FAILED,
    },
    RunState.INGEST: {RunState.TRIAGE, RunState.FAILED},
    RunState.TRIAGE: {RunState.PLAN, RunState.FAILED},
    RunState.PLAN: {RunState.SOLVE, RunState.FAILED},
    RunState.SOLVE: {
        RunState.AUTHENTICATE,
        RunState.SOLVE,
        RunState.VERIFY,
        RunState.PLAN,
        RunState.FAILED,
    },
    RunState.VERIFY: {
        RunState.AUTHENTICATE,
        RunState.REPRODUCE,
        RunState.SOLVE,
        RunState.PLAN,
        RunState.FAILED,
    },
    RunState.REPRODUCE: {RunState.SUBMIT, RunState.SOLVE, RunState.FAILED},
    RunState.SUBMIT: {
        RunState.AUTHENTICATE,
        RunState.EVIDENCE_PENDING,
        RunState.READY,
        RunState.PLAN,
        RunState.TRIAGE,
        RunState.FAILED,
    },
    RunState.EVIDENCE_PENDING: {
        RunState.WRITEUP_PENDING,
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    RunState.WRITEUP_PENDING: {
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    # Legacy states remain loadable and resume into the new post-Accepted flow.
    RunState.EVIDENCE: {
        RunState.WRITEUP_PENDING,
        RunState.DONE,
        RunState.DONE_WITH_WARNINGS,
        RunState.FAILED,
    },
    RunState.WRITEUP: {RunState.DONE, RunState.DONE_WITH_WARNINGS, RunState.FAILED},
    RunState.READY: set(),
    RunState.DONE: set(),
    RunState.DONE_WITH_WARNINGS: set(),
    RunState.FAILED: {RunState.AUTHENTICATE},
    RunState.NEEDS_AUTHENTICATION: {RunState.AUTHENTICATE},
}


class InvalidTransition(ValueError):
    pass


def require_transition(current: RunState, target: RunState) -> None:
    if target not in FORWARD_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid state transition: {current} -> {target}")
