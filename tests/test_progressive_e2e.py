import psycopg
import pytest

from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.providers.mock_unreliable import MockUnreliableProvider
from smartdialer.worker import DispatchWorker


@pytest.mark.parametrize("adapter", [MockFastProvider, MockUnreliableProvider])
def test_progressive_approval_dispatch_bridge_completion_and_wrap_up(approval_db, queue_calls, clock, adapter):
    provider = adapter(approval_db, clock=clock, start_behavior="accept")
    queue_calls(provider=provider.name)
    worker = DispatchWorker(approval_db, {provider.name: provider}, sources=(provider,), clock=clock)
    assert worker.run_once().jobs == 1
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, provider FROM call_attempts").fetchone() == ("INITIATED", provider.name)

    clock.advance(0.4 if adapter is MockFastProvider else 1.5)
    worker.run_once()
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM call_attempts").fetchone() == ("CONNECTED",)
        assert connection.execute("SELECT state FROM agents").fetchone() == ("CONNECTED",)

    clock.advance(1.5)
    worker.run_once()
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM agents").fetchone() == ("WRAP_UP",)
        assert connection.execute("SELECT status FROM borrowers").fetchone() == ("done",)
    clock.advance(1)
    assert worker.run_once().wrapped_up == 1
    with psycopg.connect(approval_db) as connection:
        trail = connection.execute(
            """
            SELECT p.requested_count, p.approved_count, c.state, c.dispatch_status,
                   c.provider_call_id IS NOT NULL, c.answered_at IS NOT NULL, c.connected_at IS NOT NULL,
                   c.terminated_at IS NOT NULL, a.state, j.state
            FROM pacing_decisions p JOIN call_attempts c ON c.decision_id = p.id
            JOIN dispatch_jobs j ON j.attempt_id = c.id JOIN agents a ON a.id = c.agent_id
            """,
        ).fetchall()
        assert trail == [(1, 1, "COMPLETED", "CONFIRMED", True, True, True, True, "AVAILABLE", "DONE")]


def test_definite_provider_rejection_releases_owners(approval_db, queue_calls, clock):
    queue_calls()
    provider = MockFastProvider(approval_db, clock=clock, start_behavior="reject")
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    worker.run_once()
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, dispatch_status, terminal_reason FROM call_attempts").fetchone() == (
            "FAILED", "REJECTED", "provider_rejected",
        )
        assert connection.execute("SELECT state FROM agents").fetchone() == ("AVAILABLE",)
        assert connection.execute("SELECT status FROM borrowers").fetchone() == ("done",)
