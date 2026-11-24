from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import psycopg

from smartdialer.providers.base import ProviderEvent, ProviderEventType, ProviderOutcome, ProviderResult

FAST_PROVIDER = "mock_fast"
StartBehavior = Literal["accept", "reject", "timeout_before", "timeout_after"]
# Event, occurrence offset and delivery offset in seconds.
Timeline = tuple[tuple[ProviderEventType, float, float], ...]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MockFastProvider:
    name = FAST_PROVIDER
    timeline: Timeline = (
        (ProviderEventType.RINGING, 0.1, 0.1),
        (ProviderEventType.ANSWERED, 0.2, 0.2),
        (ProviderEventType.CONNECTED, 0.3, 0.3),
        (ProviderEventType.COMPLETED, 0.6, 0.6),
    )

    def __init__(
        self, dsn: str, *, clock: Callable[[], datetime] = utc_now,
        start_behavior: StartBehavior | None = None, lookup_timeout: bool = False,
        cancel_timeout: bool = False, timeline: Timeline | None = None,
    ) -> None:
        self._dsn = dsn
        self._clock = clock
        self._start_behavior = start_behavior
        self._lookup_timeout = lookup_timeout
        self._cancel_timeout = cancel_timeout
        if timeline is not None:
            self.timeline = timeline

    def _behavior(self, key: str) -> StartBehavior:
        return self._start_behavior or "accept"

    def start_call(self, attempt_id: UUID, destination: str, key: str) -> ProviderResult:
        behavior = self._behavior(key)
        if behavior == "timeout_before":
            return ProviderResult(ProviderOutcome.UNKNOWN)
        now = self._clock()
        with psycopg.connect(self._dsn) as connection:
            status = "REJECTED" if behavior == "reject" else "ACCEPTED"
            call_id = f"{self.name}:{key}" if status == "ACCEPTED" else None
            inserted = connection.execute(
                """
                INSERT INTO mock_calls (provider, idempotency_key, attempt_id, destination, provider_call_id, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING idempotency_key
                """,
                (self.name, key, attempt_id, destination, call_id, status, now),
            ).fetchone()
            if inserted and status == "ACCEPTED":
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO mock_events (provider, idempotency_key, event_type, occurred_at, deliver_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        ((self.name, key, event, now + timedelta(seconds=occurs), now + timedelta(seconds=delivery))
                         for event, occurs, delivery in self.timeline),
                    )
                if behavior == "timeout_after":
                    return ProviderResult(ProviderOutcome.UNKNOWN)
            return self._lookup(connection, key, now)

    def _lookup(self, connection: psycopg.Connection, key: str, now: datetime) -> ProviderResult:
        row = connection.execute(
            """
            SELECT provider_call_id, status, created_at, cancelled_at FROM mock_calls
            WHERE provider = %s AND idempotency_key = %s
            """,
            (self.name, key),
        ).fetchone()
        if row is None:
            return ProviderResult(ProviderOutcome.ABSENT)
        call_id, status, created_at, cancelled_at = row
        accepted_at = created_at if call_id is not None else None
        if status == "CANCELLED":
            return ProviderResult(ProviderOutcome.CANCELLED, call_id, ProviderEventType.CANCELLED,
                                  cancelled_at, accepted_at)
        if status == "REJECTED":
            return ProviderResult(ProviderOutcome.REJECTED, occurred_at=created_at)
        latest = connection.execute(
            """
            SELECT event_type, occurred_at FROM mock_events
            WHERE provider = %s AND idempotency_key = %s AND occurred_at <= %s
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1
            """,
            (self.name, key, now),
        ).fetchone()
        event, occurred_at = latest or (ProviderEventType.INITIATED, created_at)
        return ProviderResult(ProviderOutcome.ACCEPTED, call_id, ProviderEventType(event), occurred_at, accepted_at)

    def lookup(self, key: str) -> ProviderResult:
        if self._lookup_timeout:
            return ProviderResult(ProviderOutcome.UNKNOWN)
        with psycopg.connect(self._dsn) as connection:
            return self._lookup(connection, key, self._clock())

    def cancel(self, key: str) -> ProviderResult:
        if self._cancel_timeout:
            return ProviderResult(ProviderOutcome.UNKNOWN)
        with psycopg.connect(self._dsn) as connection:
            now = self._clock()
            connection.execute(
                """
                INSERT INTO mock_calls (provider, idempotency_key, status, created_at, cancelled_at)
                VALUES (%s, %s, 'CANCELLED', %s, %s) ON CONFLICT DO NOTHING
                """,
                (self.name, key, now, now),
            )
            connection.execute(
                "SELECT 1 FROM mock_calls WHERE provider = %s AND idempotency_key = %s FOR UPDATE",
                (self.name, key),
            )
            now = self._clock()
            result = self._lookup(connection, key, now)
            if result.outcome != ProviderOutcome.ACCEPTED or result.observed_state in {
                ProviderEventType.COMPLETED, ProviderEventType.FAILED, ProviderEventType.CANCELLED,
                ProviderEventType.NO_ANSWER,
            }:
                return result
            connection.execute(
                "UPDATE mock_calls SET status = 'CANCELLED', cancelled_at = %s WHERE provider = %s AND idempotency_key = %s",
                (now, self.name, key),
            )
            connection.execute(
                "DELETE FROM mock_events WHERE provider = %s AND idempotency_key = %s AND occurred_at > %s",
                (self.name, key, now),
            )
            return self._lookup(connection, key, now)

    def poll_events(self, limit: int) -> list[ProviderEvent]:
        now = self._clock()
        with psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                """
                SELECT e.event_id, c.attempt_id, e.event_type, e.occurred_at
                FROM mock_events e JOIN mock_calls c USING (provider, idempotency_key)
                WHERE e.provider = %s AND e.acked_at IS NULL AND e.deliver_at <= %s AND e.occurred_at <= %s
                ORDER BY e.deliver_at, e.event_id LIMIT %s
                """,
                (self.name, now, now, limit),
            ).fetchall()
        return [ProviderEvent(self.name, str(event_id), attempt_id, ProviderEventType(kind), occurs)
                for event_id, attempt_id, kind, occurs in rows]

    def ack_event(self, event_id: str) -> None:
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "UPDATE mock_events SET acked_at = %s WHERE provider = %s AND event_id = %s AND acked_at IS NULL",
                (self._clock(), self.name, int(event_id)),
            )
