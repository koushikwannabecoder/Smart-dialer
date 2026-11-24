from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from smartdialer.allocator import AGENT_FRESHNESS
from smartdialer.domain import TERMINAL_CALL_STATES, AgentState, CallState, validate_transition

LEASE_DURATION = timedelta(seconds=5)
WRAP_UP_DURATION = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class Lease:
    attempt_id: UUID
    generation: int
    attempt_count: int

    @property
    def backoff(self) -> timedelta:
        return timedelta(seconds=2 ** min(self.attempt_count - 1, 4))


def claim_job(connection: Connection, owner: str, now: datetime) -> Lease | None:
    """Lease one due job, including one abandoned by an expired worker."""
    row = connection.execute(
        """
        WITH candidate AS (
            SELECT id FROM dispatch_jobs
            WHERE (state IN ('READY', 'RECONCILE') AND available_at <= %s)
               OR (state = 'LEASED' AND lease_until <= %s)
            ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        )
        UPDATE dispatch_jobs j SET state = 'LEASED', lease_owner = %s, lease_until = %s,
            lease_generation = lease_generation + 1, attempt_count = attempt_count + 1
        FROM candidate WHERE j.id = candidate.id
        RETURNING j.attempt_id, j.lease_generation, j.attempt_count
        """,
        (now, now, owner, now + LEASE_DURATION),
    ).fetchone()
    return Lease(*row) if row else None


@dataclass(slots=True)
class AttemptContext:
    attempt: dict
    job: dict | None
    agent: dict

    @property
    def state(self) -> CallState:
        return CallState(self.attempt["state"])

    def holds_lease(self, lease: Lease, owner: str, now: datetime) -> bool:
        return bool(self.job and self.job["state"] == "LEASED"
                    and self.job["lease_owner"] == owner and self.job["lease_generation"] == lease.generation
                    and self.job["lease_until"] > now)


def lock_attempt(connection: Connection, attempt_id: UUID) -> AttemptContext:
    # The event handler and the worker both take these locks in this order.
    with connection.cursor(row_factory=dict_row) as cursor:
        attempt = cursor.execute("SELECT * FROM call_attempts WHERE id = %s FOR UPDATE", (attempt_id,)).fetchone()
        job = cursor.execute("SELECT * FROM dispatch_jobs WHERE attempt_id = %s FOR UPDATE", (attempt_id,)).fetchone()
        agent = cursor.execute("SELECT * FROM agents WHERE id = %s FOR UPDATE", (attempt["agent_id"],)).fetchone()
    return AttemptContext(attempt, job, agent)


def set_job(connection: Connection, attempt_id: UUID, state: str, now: datetime, error: str | None = None) -> None:
    connection.execute(
        """
        UPDATE dispatch_jobs SET state = %s, available_at = %s, lease_owner = NULL, lease_until = NULL, last_error = %s
        WHERE attempt_id = %s
        """,
        (state, now, error, attempt_id),
    )


def change_agent(connection: Connection, ctx: AttemptContext, target: AgentState, now: datetime) -> None:
    state = AgentState(ctx.agent["state"])
    if state == target:
        return
    validate_transition(state, target)
    connection.execute("UPDATE agents SET state = %s, updated_at = %s WHERE id = %s", (target, now, ctx.agent["id"]))
    ctx.agent["state"] = target


def record_agent_loss(connection: Connection, ctx: AttemptContext, now: datetime) -> datetime | None:
    """Remember loss for this attempt, including when an answer is delivered later."""
    previous = ctx.attempt["agent_lost_at"]
    if ctx.state in TERMINAL_CALL_STATES:
        return previous
    agent = ctx.agent
    loss = None
    if agent["state"] in {AgentState.OFFLINE, AgentState.PAUSED}:
        loss = agent["updated_at"]
    elif agent["last_seen_at"] is None:
        loss = now
        change_agent(connection, ctx, AgentState.OFFLINE, now)
    elif agent["last_seen_at"] < now - AGENT_FRESHNESS:
        loss = agent["last_seen_at"] + AGENT_FRESHNESS
        change_agent(connection, ctx, AgentState.OFFLINE, now)
    if loss is not None:
        loss = min(loss, previous) if previous is not None else loss
        if loss != previous:
            connection.execute("UPDATE call_attempts SET agent_lost_at = %s WHERE id = %s", (loss, ctx.attempt["id"]))
            ctx.attempt["agent_lost_at"] = loss
    return ctx.attempt["agent_lost_at"]


def release_wrap_up(connection: Connection, now: datetime, limit: int) -> int:
    return connection.execute(
        """
        WITH ready AS (
            SELECT a.id FROM agents a
            WHERE a.state = 'WRAP_UP' AND a.updated_at <= %s
              AND NOT EXISTS (
                  SELECT 1 FROM call_attempts c WHERE c.agent_id = a.id
                    AND c.state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
              )
            ORDER BY a.id LIMIT %s FOR UPDATE OF a SKIP LOCKED
        )
        UPDATE agents a SET state = CASE WHEN a.last_seen_at >= %s THEN 'AVAILABLE' ELSE 'OFFLINE' END,
            updated_at = %s FROM ready WHERE a.id = ready.id
        """,
        (now - WRAP_UP_DURATION, limit, now - AGENT_FRESHNESS, now),
    ).rowcount
