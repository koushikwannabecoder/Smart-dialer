from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from smartdialer.pacing import PaceRequest, PacingSnapshot, ProgressivePacer
from smartdialer.safety import SafetyController

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_progressive_pacer_requests_one_call_per_available_agent():
    campaign_id, request_id = uuid4(), uuid4()
    request = ProgressivePacer().request(PacingSnapshot(campaign_id, 7), request_id=request_id)

    assert request == PaceRequest(request_id, campaign_id, "progressive", 7, {"available_agents": 7})
    assert ProgressivePacer().request(PacingSnapshot(campaign_id, 0)).requested_count == 0


@pytest.mark.parametrize("mode, count", [("unsupported", 1), ("progressive", -1)])
def test_unsupported_requests_are_rejected_before_accessing_postgresql(mode, count):
    with pytest.raises(ValueError):
        SafetyController("unused").approve(PaceRequest(uuid4(), uuid4(), mode, count))


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("disabled", "campaign_disabled"),
        ("empty_queue", "insufficient_capacity"),
        ("stale", "insufficient_capacity"),
        ("offline", "insufficient_capacity"),
        ("paused", "insufficient_capacity"),
        ("no_heartbeat", "insufficient_capacity"),
        ("no_demand", "no_demand"),
    ],
)
def test_unsafe_or_empty_requests_record_zero_approvals(approval_db, make_campaign, condition, reason):
    campaign = make_campaign(NOW, borrowers=int(condition != "empty_queue"), enabled=condition != "disabled")
    request = ProgressivePacer().request(PacingSnapshot(campaign, int(condition != "no_demand")))
    with psycopg.connect(approval_db) as connection:
        if condition == "stale":
            connection.execute("UPDATE agents SET last_seen_at = %s", (NOW - timedelta(seconds=6),))
        elif condition == "no_heartbeat":
            connection.execute("UPDATE agents SET last_seen_at = NULL")
        elif condition in {"offline", "paused"}:
            connection.execute("UPDATE agents SET state = %s", (condition.upper(),))

    decision = SafetyController(approval_db).approve(request, now=NOW)

    assert (decision.approved_count, decision.reason) == (0, reason)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT approved_count, reason FROM pacing_decisions").fetchone() == (0, reason)
        assert connection.execute("SELECT count(*) FROM call_attempts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (0,)


def test_approval_links_pair_and_durable_job_and_replays_original(approval_db, make_campaign):
    campaign = make_campaign(NOW, agents=2, borrowers=1)
    request = ProgressivePacer().request(PacingSnapshot(campaign, 2))
    safety = SafetyController(approval_db)

    decision = safety.approve(request, now=NOW)
    assert (decision.requested_count, decision.approved_count, decision.reason) == (2, 1, "insufficient_capacity")
    assert safety.approve(replace(request, requested_count=0), now=NOW) == decision

    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT inputs FROM pacing_decisions").fetchall() == [({"available_agents": 2},)]
        rows = connection.execute(
            """
            SELECT c.decision_id, c.state, c.dispatch_status, c.provider,
                   c.idempotency_key = c.id::text, c.approved_at,
                   a.state, b.status, j.state, j.lease_owner, j.attempt_count
            FROM call_attempts c
            JOIN agents a ON a.id = c.agent_id
            JOIN borrowers b ON b.id = c.borrower_id
            JOIN dispatch_jobs j ON j.attempt_id = c.id
            """
        ).fetchall()
        assert rows == [(decision.id, "RESERVED", "PENDING", "mock_fast", True, NOW,
                         "RESERVED", "active", "READY", None, 0)]

    other_campaign = make_campaign(NOW)
    with pytest.raises(ValueError, match="different campaign"):
        safety.approve(replace(request, campaign_id=other_campaign), now=NOW)


def test_failed_job_insert_rolls_back_decision_attempt_and_owners(approval_db, make_campaign):
    campaign = make_campaign(NOW)
    request = ProgressivePacer().request(PacingSnapshot(campaign, 1))
    with psycopg.connect(approval_db) as connection:
        connection.execute("ALTER TABLE dispatch_jobs ADD CONSTRAINT fail_ready CHECK (state <> 'READY')")

    safety = SafetyController(approval_db)
    with pytest.raises(psycopg.errors.CheckViolation, match="fail_ready"):
        safety.approve(request, now=NOW)

    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM agents").fetchall() == [("AVAILABLE",)]
        assert connection.execute("SELECT status FROM borrowers").fetchall() == [("eligible",)]
        assert connection.execute("SELECT count(*) FROM pacing_decisions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM call_attempts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (0,)
        connection.execute("ALTER TABLE dispatch_jobs DROP CONSTRAINT fail_ready")

    assert safety.approve(request, now=NOW).approved_count == 1


@pytest.mark.parametrize(("agents", "borrowers"), [(1, 2), (2, 1)])
def test_unknown_attempt_blocks_owners_even_if_their_status_looks_free(
    approval_db, make_campaign, agents, borrowers,
):
    campaign = make_campaign(NOW, agents=agents, borrowers=borrowers)
    request = ProgressivePacer().request(PacingSnapshot(campaign, 1))
    with psycopg.connect(approval_db) as connection:
        connection.execute(
            """
            INSERT INTO call_attempts (id, campaign_id, agent_id, borrower_id, state)
            SELECT %s, a.campaign_id, a.id, b.id, 'UNKNOWN'
            FROM agents a JOIN borrowers b ON b.campaign_id = a.campaign_id
            LIMIT 1
            """,
            (uuid4(),),
        )

    decision = SafetyController(approval_db).approve(request, now=NOW)
    assert decision.approved_count == 0
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT state FROM call_attempts").fetchall() == [("UNKNOWN",)]
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (0,)
