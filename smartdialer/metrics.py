from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection

from smartdialer.domain import TERMINAL_CALL_STATES


@dataclass(frozen=True, slots=True)
class CallMetrics:
    dispatch_started: int
    provider_confirmed_initiated: int
    answered: int
    bridged: int
    completed: int
    unresolved_at_end: int
    unknown_at_end: int
    answered_without_agent: int
    observed_answered_without_agent: int
    connected_agent_seconds: float
    staffed_agent_seconds: int
    utilization: float | None


def read_call_metrics(
    connection: Connection, campaign_id: UUID, start: datetime, end: datetime,
    staffed_seconds: int, departures: dict[UUID, datetime],
) -> CallMetrics:
    rows = connection.execute(
        """
        SELECT agent_id, state, dispatch_started_at, provider_accepted_at, answered_at,
               connected_at, terminated_at, agent_lost_at, answered_without_agent_at
        FROM call_attempts WHERE campaign_id = %s ORDER BY id
        """,
        (campaign_id,),
    ).fetchall()
    connected_seconds = 0.0
    without_agent = 0
    for agent_id, _, _, _, answered, connected, ended, observed_loss, incident in rows:
        departure = departures.get(agent_id)
        if incident is not None or (departure is not None and answered is not None and answered >= departure):
            without_agent += 1
        if connected is not None:
            alive_until = min(end, ended or end, observed_loss or end, departure or end)
            connected_seconds += max(0, (alive_until - max(start, connected)).total_seconds())
    return CallMetrics(
        dispatch_started=sum(row[2] is not None for row in rows),
        provider_confirmed_initiated=sum(row[3] is not None for row in rows),
        answered=sum(row[4] is not None for row in rows),
        bridged=sum(row[5] is not None for row in rows),
        completed=sum(row[1] == "COMPLETED" for row in rows),
        unresolved_at_end=sum(row[1] not in TERMINAL_CALL_STATES for row in rows),
        unknown_at_end=sum(row[1] == "UNKNOWN" for row in rows),
        answered_without_agent=without_agent,
        observed_answered_without_agent=sum(row[8] is not None for row in rows),
        connected_agent_seconds=connected_seconds,
        staffed_agent_seconds=staffed_seconds,
        utilization=connected_seconds / staffed_seconds if staffed_seconds else None,
    )
