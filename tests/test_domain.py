from datetime import datetime, timezone
from uuid import uuid4

import pytest

from smartdialer.domain import AgentState, CallState, InvalidTransition, validate_transition
from smartdialer.providers.base import ProviderEvent, ProviderEventType


def test_agent_normal_lifecycle():
    states = (
        AgentState.OFFLINE,
        AgentState.AVAILABLE,
        AgentState.RESERVED,
        AgentState.DIALING,
        AgentState.CONNECTED,
        AgentState.WRAP_UP,
        AgentState.AVAILABLE,
    )
    for current, target in zip(states, states[1:]):
        validate_transition(current, target)


def test_agent_can_disappear_during_setup_and_recover():
    validate_transition(AgentState.DIALING, AgentState.OFFLINE)
    validate_transition(AgentState.OFFLINE, AgentState.AVAILABLE)
    with pytest.raises(InvalidTransition):
        validate_transition(AgentState.OFFLINE, AgentState.RESERVED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (CallState.DISPATCHING, CallState.UNKNOWN),
        (CallState.UNKNOWN, CallState.DISPATCHING),
        (CallState.UNKNOWN, CallState.RINGING),
        (CallState.DISPATCHING, CallState.COMPLETED),
        (CallState.ANSWERED, CallState.CONNECTED),
        (CallState.RESERVED, CallState.CANCELLED),
    ],
)
def test_call_transition_allows_recovery_and_forward_provider_evidence(current, target):
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AgentState.AVAILABLE, AgentState.CONNECTED),
        (AgentState.AVAILABLE, CallState.RESERVED),
        (CallState.RESERVED, CallState.ANSWERED),
        (CallState.ANSWERED, CallState.RINGING),
        (CallState.ANSWERED, CallState.ANSWERED),
        (CallState.COMPLETED, CallState.ANSWERED),
        (CallState.FAILED, CallState.DISPATCHING),
        (CallState.CANCELLED, CallState.COMPLETED),
    ],
)
def test_invalid_transition_is_rejected(current, target):
    with pytest.raises(InvalidTransition):
        validate_transition(current, target)


def test_provider_event_requires_time_zone():
    fields = ("mock", "event-1", uuid4(), ProviderEventType.ANSWERED)
    with pytest.raises(ValueError, match="time zone"):
        ProviderEvent(*fields, datetime(2026, 1, 1))

    occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ProviderEvent(*fields, occurred_at).occurred_at == occurred_at
