from enum import StrEnum


class SessionState(StrEnum):
    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


TERMINAL_STATES = {
    SessionState.COMPLETED,
    SessionState.FAILED,
    SessionState.ABANDONED,
}

ALLOWED_TRANSITIONS = {
    SessionState.STARTING: {
        SessionState.ACTIVE,
        SessionState.FAILED,
        SessionState.ABANDONED,
    },
    SessionState.ACTIVE: {
        SessionState.DRAINING,
        SessionState.FAILED,
        SessionState.ABANDONED,
    },
    SessionState.DRAINING: {
        SessionState.COMPLETED,
        SessionState.FAILED,
        SessionState.ABANDONED,
    },
    SessionState.COMPLETED: set(),
    SessionState.FAILED: set(),
    SessionState.ABANDONED: set(),
}


class InvalidSessionTransition(ValueError):
    pass


def validate_transition(current: SessionState, next_state: SessionState) -> None:
    if next_state not in ALLOWED_TRANSITIONS[current]:
        raise InvalidSessionTransition(f"cannot transition session from {current} to {next_state}")
