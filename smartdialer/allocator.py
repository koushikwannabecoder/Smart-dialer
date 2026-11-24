from datetime import datetime, timedelta
from uuid import UUID, uuid4

from psycopg import Connection, sql
from psycopg.pq import TransactionStatus

from smartdialer.domain import AgentState, validate_transition

AGENT_FRESHNESS = timedelta(seconds=5)
DEFAULT_PROVIDER = "mock_fast"

# These predicates match the two partial unique indexes in migration 002.
_NONTERMINAL = sql.SQL("state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')")
_AVAILABLE_AGENT = sql.SQL("""
    a.campaign_id = %s AND a.state = 'AVAILABLE' AND a.last_seen_at >= %s
    AND NOT EXISTS (
        SELECT 1 FROM call_attempts WHERE agent_id = a.id AND {nonterminal}
    )
""").format(nonterminal=_NONTERMINAL)
_ELIGIBLE_BORROWER = sql.SQL("""
    b.campaign_id = %s AND b.status = 'eligible'
    AND NOT EXISTS (
        SELECT 1 FROM call_attempts WHERE borrower_id = b.id AND {nonterminal}
    )
""").format(nonterminal=_NONTERMINAL)


def count_available_agents(connection: Connection, campaign_id: UUID, now: datetime) -> int:
    row = connection.execute(
        sql.SQL("SELECT count(*) FROM agents AS a WHERE {}").format(_AVAILABLE_AGENT),
        (campaign_id, now - AGENT_FRESHNESS),
    ).fetchone()
    return row[0]


class Allocator:
    def reserve_pairs(
        self,
        connection: Connection,
        *,
        decision_id: UUID,
        campaign_id: UUID,
        limit: int,
        now: datetime,
    ) -> int:
        """Reserve pairs and enqueue jobs inside the Safety Controller transaction."""
        if connection.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("Reservation requires an open approval transaction.")

        # Every allocator locks agents before borrowers and skips rows locked by peers.
        agents = connection.execute(
            sql.SQL("""
                SELECT a.id, a.state FROM agents AS a WHERE {}
                ORDER BY a.id LIMIT %s FOR UPDATE OF a SKIP LOCKED
            """).format(_AVAILABLE_AGENT),
            (campaign_id, now - AGENT_FRESHNESS, limit),
        ).fetchall()
        if not agents:
            return 0

        borrowers = connection.execute(
            sql.SQL("""
                SELECT b.id FROM borrowers AS b WHERE {}
                ORDER BY b.id LIMIT %s FOR UPDATE OF b SKIP LOCKED
            """).format(_ELIGIBLE_BORROWER),
            (campaign_id, len(agents)),
        ).fetchall()

        for (agent_id, state), (borrower_id,) in zip(agents, borrowers):
            validate_transition(AgentState(state), AgentState.RESERVED)
            connection.execute(
                "UPDATE agents SET state = 'RESERVED', updated_at = %s WHERE id = %s",
                (now, agent_id),
            )
            connection.execute(
                "UPDATE borrowers SET status = 'active', updated_at = %s WHERE id = %s",
                (now, borrower_id),
            )
            attempt_id = uuid4()
            connection.execute(
                """
                INSERT INTO call_attempts (
                    id, campaign_id, agent_id, borrower_id, state, created_at, updated_at,
                    decision_id, provider, idempotency_key, approved_at, dispatch_status
                ) VALUES (%s, %s, %s, %s, 'RESERVED', %s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    attempt_id, campaign_id, agent_id, borrower_id, now, now,
                    decision_id, DEFAULT_PROVIDER, str(attempt_id), now,
                ),
            )
            connection.execute(
                "INSERT INTO dispatch_jobs (id, attempt_id, state, available_at) VALUES (%s, %s, 'READY', %s)",
                (uuid4(), attempt_id, now),
            )

        return len(borrowers)
