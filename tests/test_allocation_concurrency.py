from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from smartdialer.pacing import PaceRequest
from smartdialer.safety import SafetyController

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def race_approvals(dsn, requests):
    barrier = Barrier(len(requests), timeout=5)

    def approve(request):
        barrier.wait()
        return SafetyController(dsn).approve(request, now=NOW)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(approve, requests))


def assert_approvals_have_unique_pairs_and_jobs(dsn, expected):
    with psycopg.connect(dsn) as connection:
        pairs = connection.execute(
            """
            SELECT c.agent_id, c.borrower_id, j.id
            FROM call_attempts c
            JOIN pacing_decisions d ON d.id = c.decision_id AND d.campaign_id = c.campaign_id
            JOIN agents a ON a.id = c.agent_id AND a.state = 'RESERVED'
            JOIN borrowers b ON b.id = c.borrower_id AND b.status = 'active'
            JOIN dispatch_jobs j ON j.attempt_id = c.id AND j.state = 'READY'
            WHERE c.state = 'RESERVED' AND c.dispatch_status = 'PENDING'
            """
        ).fetchall()
        assert len(pairs) == expected
        assert len({agent for agent, _, _ in pairs}) == expected
        assert len({borrower for _, borrower, _ in pairs}) == expected
        assert len({job for _, _, job in pairs}) == expected
        assert connection.execute("SELECT count(*) FROM call_attempts").fetchone() == (expected,)
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (expected,)
        assert connection.execute(
            """
            SELECT d.id FROM pacing_decisions d
            LEFT JOIN call_attempts c ON c.decision_id = d.id
            GROUP BY d.id HAVING count(c.id) <> d.approved_count
            """
        ).fetchall() == []


@pytest.mark.parametrize(("agents", "borrowers"), [(1, 4), (4, 1)])
def test_two_workers_contend_for_one_owner(approval_db, make_campaign, agents, borrowers):
    campaign = make_campaign(NOW, agents=agents, borrowers=borrowers)
    requests = [PaceRequest(uuid4(), campaign, "progressive", 1) for _ in range(2)]

    decisions = race_approvals(approval_db, requests)

    assert sorted(decision.approved_count for decision in decisions) == [0, 1]
    assert_approvals_have_unique_pairs_and_jobs(approval_db, 1)


def test_oversized_parallel_requests_stay_within_fifty_agents(approval_db, make_campaign):
    campaign = make_campaign(NOW, agents=50, borrowers=100)
    requests = [PaceRequest(uuid4(), campaign, "progressive", 100) for _ in range(4)]

    decisions = race_approvals(approval_db, requests)

    assert sum(decision.approved_count for decision in decisions) == 50
    assert_approvals_have_unique_pairs_and_jobs(approval_db, 50)


def test_parallel_retries_of_same_request_commit_only_one_decision(approval_db, make_campaign):
    campaign = make_campaign(NOW, agents=3, borrowers=3)
    request = PaceRequest(uuid4(), campaign, "progressive", 1)

    first, second = race_approvals(approval_db, [request, request])

    assert first == second and first.approved_count == 1
    assert_approvals_have_unique_pairs_and_jobs(approval_db, 1)
    with psycopg.connect(approval_db) as connection:
        assert connection.execute("SELECT count(*) FROM pacing_decisions").fetchone() == (1,)


def test_locked_agent_and_borrower_are_skipped(approval_db, make_campaign):
    campaign = make_campaign(NOW, agents=2, borrowers=2)
    request = PaceRequest(uuid4(), campaign, "progressive", 2)

    with psycopg.connect(approval_db) as peer:
        locked_agent = peer.execute("SELECT id FROM agents ORDER BY id LIMIT 1 FOR UPDATE").fetchone()[0]
        locked_borrower = peer.execute("SELECT id FROM borrowers ORDER BY id LIMIT 1 FOR UPDATE").fetchone()[0]
        decision = SafetyController(approval_db).approve(request, now=NOW)
        assert decision.approved_count == 1
        agent, borrower = peer.execute("SELECT agent_id, borrower_id FROM call_attempts").fetchone()
        assert agent != locked_agent and borrower != locked_borrower

    assert_approvals_have_unique_pairs_and_jobs(approval_db, 1)


@pytest.mark.parametrize("owner", ["agent", "borrower"])
def test_partial_unique_index_blocks_duplicate_active_owner(approval_db, make_campaign, owner):
    campaign = make_campaign(NOW, agents=2, borrowers=2)
    with psycopg.connect(approval_db) as connection:
        agents = [row[0] for row in connection.execute("SELECT id FROM agents ORDER BY id")]
        borrowers = [row[0] for row in connection.execute("SELECT id FROM borrowers ORDER BY id")]
        sql = "INSERT INTO call_attempts (id, campaign_id, agent_id, borrower_id, state) VALUES (%s, %s, %s, %s, %s)"
        connection.execute(sql, (uuid4(), campaign, agents[0], borrowers[0], "UNKNOWN"))
        agent = agents[0 if owner == "agent" else 1]
        borrower = borrowers[0 if owner == "borrower" else 1]
        with pytest.raises(psycopg.errors.UniqueViolation) as error, connection.transaction():
            connection.execute(sql, (uuid4(), campaign, agent, borrower, "RESERVED"))
        assert error.value.diag.constraint_name == f"call_attempts_active_{owner}_idx"

        connection.execute("UPDATE call_attempts SET state = 'COMPLETED'")
        connection.execute(sql, (uuid4(), campaign, agent, borrower, "RESERVED"))
