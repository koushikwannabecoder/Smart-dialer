import os
import subprocess
import sys
from uuid import uuid4

import psycopg

from smartdialer.pacing import PaceRequest
from smartdialer.providers.base import ProviderEventType as Event
from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.safety import SafetyController
from smartdialer.worker import DispatchWorker


def test_expired_untouched_lease_recovers_after_worker_exit(approval_db, queue_calls, clock):
    queue_calls()
    provider = MockFastProvider(approval_db, clock=clock)
    old = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    stale = old.claim_job()
    assert stale.generation == 1
    clock.advance(5)
    replacement = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    lease = replacement.claim_job()
    assert (lease.attempt_id, lease.generation) == (stale.attempt_id, 2)
    old.dispatch(stale)
    replacement.dispatch(lease)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, dispatch_status FROM call_attempts").fetchall() == [("INITIATED", "CONFIRMED")]
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (1,)


def test_process_dies_after_provider_commit_replacement_finds_original(approval_db, queue_calls, clock):
    queue_calls()
    child = """
import os
from datetime import datetime
from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.worker import DispatchWorker

class CrashAfterCommit(MockFastProvider):
    def start_call(self, *args):
        super().start_call(*args)
        os._exit(71)

dsn = os.environ['CRASH_TEST_DSN']
clock = lambda: datetime.fromisoformat(os.environ['CRASH_TEST_TIME'])
provider = CrashAfterCommit(dsn, clock=clock, timeline=())
worker = DispatchWorker(dsn, {provider.name: provider}, clock=clock)
worker.dispatch(worker.claim_job())
"""
    result = subprocess.run(
        [sys.executable, "-c", child], timeout=10, check=False,
        env={**os.environ, "CRASH_TEST_DSN": approval_db, "CRASH_TEST_TIME": clock().isoformat()},
    )
    assert result.returncode == 71
    with psycopg.connect(approval_db) as connection:
        (key,) = connection.execute("SELECT idempotency_key FROM mock_calls").fetchone()
        assert connection.execute("SELECT state, provider_call_id FROM call_attempts").fetchall() == [("DISPATCHING", None)]
    clock.advance(5)
    replacement_provider = MockFastProvider(approval_db, clock=clock)
    replacement = DispatchWorker(approval_db, {replacement_provider.name: replacement_provider}, clock=clock)
    replacement.dispatch(replacement.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, provider_call_id FROM call_attempts").fetchall() == [("INITIATED", f"mock_fast:{key}")]
        assert connection.execute("SELECT idempotency_key FROM mock_calls").fetchall() == [(key,)]
        assert connection.execute("SELECT lease_generation FROM dispatch_jobs").fetchone() == (2,)


def test_expired_worker_cannot_resurrect_cancelled_key_or_overwrite_state(approval_db, queue_calls, clock):
    queue_calls()

    class DelayedStart(MockFastProvider):
        def start_call(self, *args):
            clock.advance(6)
            replacement_provider = MockFastProvider(approval_db, clock=clock)
            replacement = DispatchWorker(approval_db, {self.name: replacement_provider}, clock=clock)
            replacement.dispatch(replacement.claim_job())
            return super().start_call(*args)

    provider = DelayedStart(approval_db, clock=clock)
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    worker.dispatch(worker.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, provider_call_id FROM call_attempts").fetchall() == [("CANCELLED", None)]
        assert connection.execute("SELECT state, lease_generation FROM dispatch_jobs").fetchall() == [("DONE", 2)]
        assert connection.execute("SELECT status, provider_call_id FROM mock_calls").fetchall() == [("CANCELLED", None)]
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("eligible",)]


def test_late_accepted_response_cannot_write_through_replacement_lease(approval_db, queue_calls, clock):
    queue_calls()
    replacement_provider = MockFastProvider(approval_db, clock=clock)
    replacement = DispatchWorker(approval_db, {replacement_provider.name: replacement_provider}, clock=clock)
    replacement_lease = None

    class DelayedResponse(MockFastProvider):
        def start_call(self, *args):
            nonlocal replacement_lease
            result = super().start_call(*args)
            clock.advance(5)
            replacement_lease = replacement.claim_job()
            return result

    provider = DelayedResponse(approval_db, clock=clock, timeline=())
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    worker.dispatch(worker.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, provider_call_id FROM call_attempts").fetchone() == ("DISPATCHING", None)
        assert connection.execute("SELECT state, lease_generation FROM dispatch_jobs").fetchone() == ("LEASED", 2)
    replacement.dispatch(replacement_lease)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, dispatch_status FROM call_attempts").fetchone() == ("INITIATED", "CONFIRMED")
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (1,)


def test_authoritative_absence_allows_safe_same_key_retry(approval_db, queue_calls, clock):
    queue_calls()
    timeout = MockFastProvider(approval_db, clock=clock, start_behavior="timeout_before")
    worker = DispatchWorker(approval_db, {timeout.name: timeout}, clock=clock)
    lease = worker.claim_job()
    worker.dispatch(lease)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, dispatch_status FROM call_attempts").fetchall() == [("UNKNOWN", "UNKNOWN")]
    clock.advance(1)
    provider = MockFastProvider(approval_db, clock=clock)
    replacement = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    replacement.dispatch(replacement.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM call_attempts").fetchall() == [("INITIATED",)]
        assert connection.execute("SELECT idempotency_key FROM mock_calls").fetchall() == [(str(lease.attempt_id),)]


def test_outage_blocks_approvals_and_preapproved_starts_without_releasing_unknowns(
    approval_db, queue_calls, make_campaign, clock,
):
    queue_calls(agents=4)
    provider = MockFastProvider(approval_db, clock=clock, start_behavior="timeout_after", lookup_timeout=True)
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    assert worker.run_once().jobs == 4
    other = make_campaign(clock())
    request = PaceRequest(uuid4(), other, "progressive", 1)
    decision = SafetyController(approval_db).approve(request, now=clock())
    assert (decision.approved_count, decision.reason) == (0, "provider_unavailable")
    clock.advance(1)
    worker.run_once()
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, count(*) FROM call_attempts GROUP BY state ORDER BY state").fetchall() == [
            ("RESERVED", 1), ("UNKNOWN", 3),
        ]
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM borrowers WHERE status = 'active'").fetchone() == (4,)
        assert connection.execute("SELECT count(*) FROM agents WHERE state = 'DIALING'").fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM dispatch_jobs WHERE state = 'RECONCILE' AND available_at > %s", (clock(),),
        ).fetchone() == (4,)

    recovered = MockFastProvider(approval_db, clock=clock)
    replacement = DispatchWorker(approval_db, {recovered.name: recovered}, sources=(recovered,), clock=clock)
    assert replacement.run_once().events == 12
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT count(*) FROM call_attempts WHERE state = 'COMPLETED'").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (3,)


def test_loss_before_dispatch_cancels_locally(approval_db, queue_calls, clock):
    queue_calls()
    clock.advance(6)
    provider = MockFastProvider(approval_db, clock=clock)
    worker = DispatchWorker(approval_db, {provider.name: provider}, clock=clock)
    worker.dispatch(worker.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, dispatch_started_at FROM call_attempts").fetchall() == [("CANCELLED", None)]
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (0,)
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("eligible",)]
        assert connection.execute("SELECT state FROM agents").fetchall() == [("OFFLINE",)]


def test_loss_after_dispatch_keeps_ownership_until_cancel_and_records_unserved_answer(approval_db, queue_calls, clock):
    queue_calls()
    timeline = ((Event.ANSWERED, 1, 1), (Event.CONNECTED, 1.5, 1.5), (Event.COMPLETED, 10, 10))
    provider = MockFastProvider(approval_db, clock=clock, timeline=timeline, cancel_timeout=True)
    worker = DispatchWorker(approval_db, {provider.name: provider}, sources=(provider,), clock=clock)
    worker.dispatch(worker.claim_job())
    clock.advance(0.5)
    with psycopg.connect(approval_db) as connection:
        connection.execute("UPDATE agents SET state = 'OFFLINE', updated_at = %s", (clock(),))
    clock.advance(1)
    worker.run_once()
    with psycopg.connect(approval_db) as connection:
        assert connection.execute(
            "SELECT state, answered_without_agent_at IS NOT NULL, connected_at FROM call_attempts",
        ).fetchall() == [("ANSWERED", True, None)]
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("active",)]
    clock.advance(2)
    recovered = MockFastProvider(approval_db, clock=clock)
    replacement = DispatchWorker(approval_db, {recovered.name: recovered}, clock=clock)
    replacement.dispatch(replacement.claim_job())
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, answered_without_agent_at IS NOT NULL FROM call_attempts").fetchall() == [("CANCELLED", True)]
        assert connection.execute("SELECT state FROM agents").fetchall() == [("OFFLINE",)]
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("done",)]
