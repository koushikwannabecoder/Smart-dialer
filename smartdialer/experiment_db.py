from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from smartdialer.db import migrate


@contextmanager
def isolated_schema(dsn: str) -> Iterator[str]:
    """Give each run its own PostgreSQL tables and remove only those tables on exit."""
    name = f"smartdialer_run_{uuid4().hex}"
    identifier = sql.Identifier(name)
    options = conninfo_to_dict(dsn).get("options", "")
    isolated = make_conninfo(dsn, options=f"{options} -c search_path={name} -c statement_timeout=10s")
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(identifier))
        try:
            migrate(isolated)
            yield isolated
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))


def seed_campaign(
    dsn: str, *, mode: str, provider: str, agent_count: int, borrower_count: int, now: datetime,
) -> tuple[UUID, list[UUID]]:
    """Seed stable owner ordering in a fresh experiment schema."""
    campaign_id = UUID(int=1)
    agents = [UUID(int=index + 1) for index in range(agent_count)]
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO campaigns (id, name, mode, provider, created_at) VALUES (%s, %s, %s, %s, %s)",
            (campaign_id, "experiment", mode, provider, now),
        )
        cursor.executemany(
            "INSERT INTO agents (id, campaign_id, state, last_seen_at, updated_at) VALUES (%s, %s, 'AVAILABLE', %s, %s)",
            ((agent_id, campaign_id, now, now) for agent_id in agents),
        )
        cursor.executemany(
            "INSERT INTO borrowers (id, campaign_id, destination, status, updated_at) VALUES (%s, %s, %s, 'eligible', %s)",
            ((UUID(int=index + 1), campaign_id, f"borrower-{index + 1}", now) for index in range(borrower_count)),
        )
    return campaign_id, agents
