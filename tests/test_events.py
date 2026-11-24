from datetime import timedelta

import psycopg
import pytest

from smartdialer.events import process_one_event, record_event
from smartdialer.providers.base import ProviderEvent, ProviderEventType as Event
from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.worker import DispatchWorker


@pytest.fixture
def started(approval_db, queue_calls, clock):
    queue_calls()
    provider = MockFastProvider(approval_db, clock=clock, timeline=())
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    lease = worker.claim_job()
    worker.dispatch(lease)
    return lease.attempt_id


def emit(dsn, attempt, kind, event_id, occurs, received):
    record_event(dsn, ProviderEvent("mock_fast", event_id, attempt, kind, occurs), received)


def test_duplicate_answer_has_one_effect_and_completion_releases_once(approval_db, started, clock):
    clock.advance(1)
    answered_at = clock()
    emit(approval_db, started, Event.ANSWERED, "answer-1", answered_at, clock())
    assert process_one_event(approval_db, clock())
    clock.advance(1)
    emit(approval_db, started, Event.ANSWERED, "answer-1", answered_at, clock())
    emit(approval_db, started, Event.ANSWERED, "answer-2", answered_at, clock())
    assert process_one_event(approval_db, clock())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, answered_at, updated_at FROM call_attempts").fetchall() == [
            ("ANSWERED", answered_at, answered_at),
        ]
        assert connection.execute("SELECT state FROM agents").fetchall() == [("DIALING",)]
        assert connection.execute("SELECT count(*) FROM provider_events").fetchone() == (2,)

    emit(approval_db, started, Event.COMPLETED, "end-1", clock(), clock())
    assert process_one_event(approval_db, clock())
    ended = clock()
    clock.advance(1)
    emit(approval_db, started, Event.COMPLETED, "end-2", ended, clock())
    assert process_one_event(approval_db, clock())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, updated_at FROM agents").fetchall() == [("AVAILABLE", ended)]
        assert connection.execute("SELECT status, updated_at FROM borrowers").fetchall() == [("done", ended)]
        assert connection.execute("SELECT state FROM dispatch_jobs").fetchall() == [("DONE",)]


def test_completion_first_and_late_progress_stay_terminal(approval_db, started, clock):
    clock.advance(3)
    end = clock()
    emit(approval_db, started, Event.COMPLETED, "end", end, clock())
    assert process_one_event(approval_db, clock())
    clock.advance(0.1)
    emit(approval_db, started, Event.ANSWERED, "answer", end - timedelta(seconds=1), clock())
    emit(approval_db, started, Event.RINGING, "ring", end - timedelta(seconds=2), clock())
    assert process_one_event(approval_db, clock())
    assert process_one_event(approval_db, clock())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, answered_at, connected_at, terminated_at FROM call_attempts").fetchall() == [
            ("COMPLETED", end - timedelta(seconds=1), None, end),
        ]
        assert connection.execute("SELECT state, updated_at FROM agents").fetchall() == [("AVAILABLE", end)]


@pytest.mark.parametrize("answer_processed", [False, True])
def test_completed_after_restart_handles_pending_or_already_applied_answer(
    approval_db, started, clock, answer_processed,
):
    clock.advance(1)
    emit(approval_db, started, Event.ANSWERED, "answer", clock(), clock())
    if answer_processed:
        assert process_one_event(approval_db, clock())
    clock.advance(1)
    emit(approval_db, started, Event.COMPLETED, "end", clock(), clock())
    replacement = DispatchWorker(approval_db, {}, clock=clock)
    assert replacement.run_once().events == (1 if answer_processed else 2)
    assert replacement.run_once().events == 0
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, answered_at IS NOT NULL FROM call_attempts").fetchall() == [("COMPLETED", True)]
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("done",)]


def test_failure_before_inbox_mark_rolls_back_terminal_and_release(approval_db, started, clock):
    clock.advance(1)
    emit(approval_db, started, Event.COMPLETED, "end", clock(), clock())
    with psycopg.connect(approval_db) as connection:
        connection.execute("ALTER TABLE provider_events ADD CONSTRAINT inject_failure CHECK (processed_at IS NULL)")
    with pytest.raises(psycopg.errors.CheckViolation, match="inject_failure"):
        process_one_event(approval_db, clock())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM call_attempts").fetchone() == ("INITIATED",)
        assert connection.execute("SELECT state FROM agents").fetchone() == ("DIALING",)
        assert connection.execute("SELECT status FROM borrowers").fetchone() == ("active",)
        connection.execute("ALTER TABLE provider_events DROP CONSTRAINT inject_failure")
    assert process_one_event(approval_db, clock())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM call_attempts").fetchone() == ("COMPLETED",)
