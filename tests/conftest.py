import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from smartdialer.db import migrate
from smartdialer.pacing import PaceRequest
from smartdialer.safety import SafetyController


@pytest.fixture
def test_dsn():
    """Isolate each integration test in its own schema."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests.")

    schema_name = f"smartdialer_test_{uuid4().hex}"
    schema = sql.Identifier(schema_name)
    options = conninfo_to_dict(dsn).get("options", "")
    isolated_dsn = make_conninfo(dsn, options=f"{options} -c search_path={schema_name}")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            yield isolated_dsn
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))


@pytest.fixture
def approval_db(test_dsn):
    migrate(test_dsn)
    options = conninfo_to_dict(test_dsn)["options"]
    return make_conninfo(test_dsn, options=f"{options} -c statement_timeout=5s")


@pytest.fixture
def make_campaign(approval_db):
    def create(now, *, agents=1, borrowers=1, enabled=True, mode="progressive"):
        campaign_id = uuid4()
        with psycopg.connect(approval_db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO campaigns (id, name, mode, enabled) VALUES (%s, %s, %s, %s)",
                (campaign_id, str(campaign_id), mode, enabled),
            )
            cursor.executemany(
                "INSERT INTO agents (id, campaign_id, state, last_seen_at) VALUES (%s, %s, 'AVAILABLE', %s)",
                ((uuid4(), campaign_id, now) for _ in range(agents)),
            )
            cursor.executemany(
                "INSERT INTO borrowers (id, campaign_id, destination, status) VALUES (%s, %s, %s, 'eligible')",
                ((uuid4(), campaign_id, f"test-{index}") for index in range(borrowers)),
            )
        return campaign_id

    return create


@pytest.fixture
def clock():
    class ManualClock:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += timedelta(seconds=seconds)

    return ManualClock()


@pytest.fixture
def queue_calls(approval_db, make_campaign, clock):
    def queue(*, agents=1, provider="mock_fast"):
        campaign_id = make_campaign(clock(), agents=agents, borrowers=agents)
        with psycopg.connect(approval_db) as connection:
            connection.execute("UPDATE campaigns SET provider = %s WHERE id = %s", (provider, campaign_id))
        request = PaceRequest(uuid4(), campaign_id, "progressive", agents)
        assert SafetyController(approval_db).approve(request, now=clock()).approved_count == agents
        return campaign_id

    return queue
