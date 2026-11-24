from uuid import uuid4

import psycopg
import pytest

from smartdialer.providers.base import ProviderEventType as Event, ProviderOutcome as Outcome
from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.providers.mock_unreliable import MockUnreliableProvider


def test_mock_start_and_timeline_survive_restart(approval_db, clock):
    attempt_id = uuid4()
    key = str(attempt_id)
    first = MockFastProvider(approval_db, clock=clock).start_call(attempt_id, "demo", key)
    restarted = MockFastProvider(approval_db, clock=clock)
    assert first == restarted.start_call(attempt_id, "demo", key) == restarted.lookup(key)
    clock.advance(1)
    events = restarted.poll_events(10)
    assert [event.event_type for event in events] == [Event.RINGING, Event.ANSWERED, Event.CONNECTED, Event.COMPLETED]
    restarted.ack_event(events[0].event_id)
    assert MockFastProvider(approval_db, clock=clock).poll_events(10) == events[1:]


def test_unreliable_mock_times_out_after_accept_and_delivers_duplicates_out_of_order(approval_db, clock):
    attempt_id = uuid4()
    provider = MockUnreliableProvider(approval_db, clock=clock, start_behavior="timeout_after")
    assert provider.start_call(attempt_id, "demo", str(attempt_id)).outcome == Outcome.UNKNOWN
    assert provider.lookup(str(attempt_id)).outcome == Outcome.ACCEPTED
    clock.advance(3)
    assert [event.event_type for event in provider.poll_events(10)] == [
        Event.ANSWERED, Event.ANSWERED, Event.CONNECTED, Event.COMPLETED, Event.RINGING,
    ]


@pytest.mark.parametrize(("behavior", "outcome", "lookup"), [
    ("reject", Outcome.REJECTED, Outcome.REJECTED),
    ("timeout_before", Outcome.UNKNOWN, Outcome.ABSENT),
])
def test_rejection_and_pre_acceptance_timeout_are_distinct(approval_db, clock, behavior, outcome, lookup):
    attempt_id = uuid4()
    provider = MockUnreliableProvider(approval_db, clock=clock, start_behavior=behavior)
    assert provider.start_call(attempt_id, "demo", str(attempt_id)).outcome == outcome
    assert provider.lookup(str(attempt_id)).outcome == lookup


def test_cancel_before_acceptance_prevents_a_late_start(approval_db, clock):
    attempt_id = uuid4()
    key = str(attempt_id)
    provider = MockFastProvider(approval_db, clock=clock)
    assert provider.cancel(key).outcome == Outcome.CANCELLED
    assert MockFastProvider(approval_db, clock=clock).start_call(attempt_id, "demo", key).outcome == Outcome.CANCELLED
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT provider_call_id FROM mock_calls").fetchall() == [(None,)]
        assert connection.execute("SELECT count(*) FROM mock_events").fetchone() == (0,)
