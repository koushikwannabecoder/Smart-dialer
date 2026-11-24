from datetime import datetime, timedelta
from uuid import UUID

from psycopg import Connection

from smartdialer.allocator import AGENT_FRESHNESS, count_available_agents
from smartdialer.pacing import HISTORY_SIZE, HISTORY_WINDOW_SECONDS, PacingSnapshot


def read_pacing_snapshot(connection: Connection, campaign_id: UUID, *, now: datetime) -> PacingSnapshot:
    """Read the forecast inputs without claiming agents, attempts, or jobs."""
    provider_info = connection.execute(
        """
        SELECT c.provider, h.blocked_until IS NULL OR h.blocked_until <= %s
        FROM campaigns c JOIN provider_health h ON h.provider = c.provider WHERE c.id = %s
        """,
        (now, campaign_id),
    ).fetchone()
    if provider_info is None:
        raise ValueError(f"Unknown campaign {campaign_id}.")
    provider, provider_available = provider_info
    staffed, = connection.execute(
        """
        SELECT count(*) FROM agents
        WHERE campaign_id = %s AND state NOT IN ('OFFLINE', 'PAUSED') AND last_seen_at >= %s
        """,
        (campaign_id, now - AGENT_FRESHNESS),
    ).fetchone()
    connected, pending, unknown = connection.execute(
        """
        SELECT count(*) FILTER (WHERE state = 'CONNECTED'),
               count(*) FILTER (WHERE state NOT IN ('CONNECTED', 'UNKNOWN')),
               count(*) FILTER (WHERE state = 'UNKNOWN')
        FROM call_attempts WHERE campaign_id = %s AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
        """,
        (campaign_id,),
    ).fetchone()
    answers, no_answers, mean_talk = connection.execute(
        """
        WITH recent AS (
            SELECT terminal_reason = 'no_answer' AS no_answer, connected_at, terminated_at
            FROM call_attempts
            WHERE campaign_id = %s AND provider = %s AND dispatch_status = 'CONFIRMED'
              AND state IN ('COMPLETED', 'FAILED', 'CANCELLED') AND terminated_at BETWEEN %s AND %s
              AND (terminal_reason = 'no_answer' OR
                   (state <> 'FAILED' AND (answered_at IS NOT NULL OR connected_at IS NOT NULL)))
            ORDER BY terminated_at DESC, id DESC LIMIT %s
        )
        SELECT count(*) FILTER (WHERE no_answer IS NOT TRUE), count(*) FILTER (WHERE no_answer),
               avg(extract(epoch FROM terminated_at - connected_at))
                   FILTER (WHERE no_answer IS NOT TRUE AND terminated_at > connected_at)::double precision
        FROM recent
        """,
        (campaign_id, provider, now - timedelta(seconds=HISTORY_WINDOW_SECONDS), now, HISTORY_SIZE),
    ).fetchone()
    return PacingSnapshot(
        campaign_id=campaign_id, available_agents=count_available_agents(connection, campaign_id, now),
        staffed_agents=staffed, connected_calls=connected, pending_setups=pending, unknown_attempts=unknown,
        known_answers=answers, known_no_answers=no_answers, mean_talk_seconds=mean_talk,
        provider_available=provider_available, provider=provider, observed_at=now,
    )
