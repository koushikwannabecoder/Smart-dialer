from enum import StrEnum


class AgentState(StrEnum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


class CallState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHING = "DISPATCHING"
    UNKNOWN = "UNKNOWN"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_CALL_STATES = frozenset({CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED})

_AGENT_TRANSITIONS = {
    AgentState.OFFLINE: {AgentState.AVAILABLE},
    AgentState.AVAILABLE: {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.RESERVED: {
        AgentState.DIALING, AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE,
    },
    AgentState.DIALING: {
        AgentState.CONNECTED, AgentState.WRAP_UP, AgentState.AVAILABLE, AgentState.PAUSED,
        AgentState.OFFLINE,
    },
    AgentState.CONNECTED: {AgentState.WRAP_UP, AgentState.OFFLINE},
    AgentState.WRAP_UP: {AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.PAUSED: {AgentState.AVAILABLE, AgentState.OFFLINE},
}

_PROVIDER_CALL_STATES = (
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.CONNECTED,
)

_CALL_TRANSITIONS = {
    CallState.RESERVED: {CallState.DISPATCHING, CallState.CANCELLED},
    CallState.DISPATCHING: {CallState.UNKNOWN, *_PROVIDER_CALL_STATES, *TERMINAL_CALL_STATES},
    CallState.UNKNOWN: {CallState.DISPATCHING, *_PROVIDER_CALL_STATES, *TERMINAL_CALL_STATES},
    **{
        state: {*_PROVIDER_CALL_STATES[index + 1:], *TERMINAL_CALL_STATES}
        for index, state in enumerate(_PROVIDER_CALL_STATES)
    },
}


class InvalidTransition(ValueError):
    """The requested state change is outside the lifecycle."""


def validate_transition(current: AgentState | CallState, target: AgentState | CallState) -> None:
    """Validate a state change. The caller supplies evidence and handles repeated events."""
    if isinstance(current, AgentState) and isinstance(target, AgentState):
        allowed = _AGENT_TRANSITIONS[current]
    elif isinstance(current, CallState) and isinstance(target, CallState):
        allowed = _CALL_TRANSITIONS.get(current, frozenset())
    else:
        raise InvalidTransition("Both states must be members of the same lifecycle.")

    if target not in allowed:
        raise InvalidTransition(f"Invalid {type(current).__name__} transition: {current} -> {target}")
