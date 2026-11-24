from datetime import timedelta
from math import ceil
from uuid import uuid4

import psycopg
import pytest

from smartdialer.pacing import PaceRequest, PredictivePacer
from smartdialer.pacing_stats import read_pacing_snapshot
from smartdialer.providers.base import ProviderEventType
from smartdialer.providers.mock_fast import MockFastProvider
from smartdialer.safety import SafetyController
from smartdialer.worker import DispatchWorker


def add_history(dsn, campaign_id, ended, kinds):
    """Seed terminal attempts with known timestamps for the statistics query."""
    decision_id = uuid4()
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        agent, = cursor.execute("SELECT id FROM agents WHERE campaign_id = %s LIMIT 1", (campaign_id,)).fetchone()
        borrower, = cursor.execute("SELECT id FROM borrowers WHERE campaign_id = %s LIMIT 1", (campaign_id,)).fetchone()
        cursor.execute(
            """
            INSERT INTO pacing_decisions (id, campaign_id, mode, requested_count, approved_count, reason, inputs)
            VALUES (%s, %s, 'predictive', %s, %s, 'approved', '{}')
            """,
            (decision_id, campaign_id, len(kinds), len(kinds)),
        )
        for index, kind in enumerate(kinds):
            attempt_id = uuid4()
            terminal = ended - timedelta(seconds=index)
            state, reason, dispatch, connected_seconds = {
                "answer": ("COMPLETED", "completed", "CONFIRMED", 40),
                "no_answer": ("FAILED", "no_answer", "CONFIRMED", None),
                "failure": ("FAILED", "failed", "CONFIRMED", None),
                "rejection": ("FAILED", "provider_rejected", "REJECTED", None),
            }[kind]
            connected = terminal - timedelta(seconds=connected_seconds) if connected_seconds is not None else None
            cursor.execute(
                """
                INSERT INTO call_attempts (id, campaign_id, agent_id, borrower_id, state, decision_id, provider,
                    idempotency_key, approved_at, dispatch_status, connected_at, terminated_at, terminal_reason)
                VALUES (%s, %s, %s, %s, %s, %s, 'mock_fast', %s, %s, %s, %s, %s, %s)
                """,
                (attempt_id, campaign_id, agent, borrower, state, decision_id, str(attempt_id),
                 terminal - timedelta(minutes=2), dispatch, connected, terminal, reason),
            )


def snapshot(dsn, campaign_id, now):
    with psycopg.connect(dsn) as connection:
        connection.read_only = True
        return read_pacing_snapshot(connection, campaign_id, now=now)


def test_forecast_and_database_explain_sixteen_requested_but_eight_approved(approval_db, make_campaign, clock):
    campaign = make_campaign(clock(), agents=20, borrowers=25, mode="predictive")
    add_history(approval_db, campaign, clock() - timedelta(seconds=60), ["answer", "no_answer"] * 5)
    safety = SafetyController(approval_db)
    active = safety.approve(PaceRequest(uuid4(), campaign, "predictive", 12), now=clock())
    with psycopg.connect(approval_db) as connection:
        connection.execute(
            """
            WITH connected AS (
                UPDATE call_attempts SET state = 'CONNECTED', dispatch_status = 'CONFIRMED'
                WHERE id IN (SELECT id FROM call_attempts WHERE decision_id = %s ORDER BY id LIMIT 8)
                RETURNING agent_id
            )
            UPDATE agents SET state = 'CONNECTED' WHERE id IN (SELECT agent_id FROM connected)
            """,
            (active.id,),
        )

    observed = snapshot(approval_db, campaign, clock())
    assert (observed.available_agents, observed.staffed_agents, observed.connected_calls, observed.pending_setups) == (8, 20, 8, 4)
    policy_request = PredictivePacer().request(observed)
    decision = safety.approve(policy_request, now=clock())

    assert (decision.requested_count, decision.approved_count, decision.reason) == (16, 8, "insufficient_capacity")
    with psycopg.connect(approval_db) as connection:
        inputs, = connection.execute("SELECT inputs FROM pacing_decisions WHERE id = %s", (decision.id,)).fetchone()
        assert inputs == policy_request.inputs
        assert ceil(inputs["expected_answers_needed"] / inputs["answer_rate_divisor"]) == 16
        assert connection.execute(
            """
            SELECT count(*), count(DISTINCT a.agent_id), count(DISTINCT a.borrower_id)
            FROM call_attempts a JOIN dispatch_jobs j ON j.attempt_id = a.id
            WHERE a.decision_id = %s AND a.state = 'RESERVED' AND j.state = 'READY'
            """,
            (decision.id,),
        ).fetchone() == (8, 8, 8)


def test_recent_outcomes_shift_from_seventy_to_ten_percent_without_oversubscribing(approval_db, make_campaign, clock):
    campaign = make_campaign(clock(), agents=10, borrowers=15, mode="predictive")
    add_history(approval_db, campaign, clock() - timedelta(seconds=90), ["answer"] * 35 + ["no_answer"] * 15)
    initial = PredictivePacer().request(snapshot(approval_db, campaign, clock()))
    add_history(approval_db, campaign, clock() - timedelta(seconds=10), ["answer"] * 5 + ["no_answer"] * 45)
    add_history(approval_db, campaign, clock(), ["failure", "rejection"])
    shifted = PredictivePacer().request(snapshot(approval_db, campaign, clock()))

    assert (initial.inputs["answer_rate"], initial.requested_count) == (0.7, 13)
    assert (shifted.inputs["answer_rate"], shifted.requested_count) == (0.1, 45)
    assert shifted.inputs["sample_count"] == 50
    assert SafetyController(approval_db).approve(shifted, now=clock()).approved_count == 10


def test_expired_outcomes_trigger_progressive_fallback(approval_db, make_campaign, clock):
    campaign = make_campaign(clock(), agents=3, borrowers=3, mode="predictive")
    add_history(approval_db, campaign, clock() - timedelta(minutes=31), ["answer"] * 10)

    request = PredictivePacer().request(snapshot(approval_db, campaign, clock()))

    assert request.inputs["sample_count"] == 0
    assert request.inputs["strategy"] == "progressive_fallback"
    assert SafetyController(approval_db).approve(request, now=clock()).approved_count == 3


@pytest.mark.parametrize(("change", "approved", "reason"), [
    ("agent_offline", 2, "insufficient_capacity"),
    ("all_agents_offline", 0, "insufficient_capacity"),
    ("provider_cooldown", 0, "provider_unavailable"),
])
def test_predictive_snapshot_has_no_write_path_and_safety_rechecks_current_state(
    approval_db, make_campaign, clock, change, approved, reason,
):
    campaign = make_campaign(clock(), agents=3, borrowers=3, mode="predictive")
    request = PredictivePacer().request(snapshot(approval_db, campaign, clock()))
    assert request.requested_count == 3
    with psycopg.connect(approval_db) as connection:
        for table in ("pacing_decisions", "call_attempts", "dispatch_jobs", "mock_calls"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        if change == "provider_cooldown":
            connection.execute(
                "UPDATE provider_health SET blocked_until = %s WHERE provider = 'mock_fast'",
                (clock() + timedelta(seconds=30),),
            )
        elif change == "all_agents_offline":
            connection.execute("UPDATE agents SET state = 'OFFLINE'")
        else:
            connection.execute("UPDATE agents SET state = 'OFFLINE' WHERE id = (SELECT id FROM agents LIMIT 1)")

    decision = SafetyController(approval_db).approve(request, now=clock())
    assert (decision.approved_count, decision.reason) == (approved, reason)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (approved,)
        assert connection.execute("SELECT count(*) FROM mock_calls").fetchone() == (0,)


@pytest.mark.parametrize("via_callback", [True, False])
def test_explicit_no_answer_is_counted_through_callback_or_lookup(approval_db, make_campaign, clock, via_callback):
    campaign = make_campaign(clock(), mode="predictive")
    policy_request = PredictivePacer().request(snapshot(approval_db, campaign, clock()))
    assert SafetyController(approval_db).approve(policy_request, now=clock()).approved_count == 1
    provider = MockFastProvider(approval_db, clock=clock, timeline=((ProviderEventType.NO_ANSWER, 0.2, 0.2),))
    worker = DispatchWorker(
        approval_db, {provider.name: provider}, sources=(provider,) if via_callback else (), clock=clock,
    )
    worker.run_once()
    clock.advance(1)
    worker.run_once()

    observed = snapshot(approval_db, campaign, clock())
    assert (observed.known_answers, observed.known_no_answers, observed.unknown_attempts) == (0, 1, 0)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state, terminal_reason FROM call_attempts").fetchone() == ("FAILED", "no_answer")
