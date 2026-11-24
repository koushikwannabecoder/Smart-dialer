from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from smartdialer.allocator import Allocator
from smartdialer.pacing import DIAL_MODES, PaceRequest

MAX_APPROVAL_BATCH = 50


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    id: UUID
    campaign_id: UUID
    mode: str
    requested_count: int
    approved_count: int
    reason: str


class SafetyController:
    def __init__(self, dsn: str, *, new_id: Callable[[], UUID] = uuid4) -> None:
        self._dsn = dsn
        self._allocator = Allocator(new_id)

    def approve(
        self, request: PaceRequest, *, now: datetime | None = None,
        connection: psycopg.Connection | None = None,
    ) -> ApprovalDecision:
        """Persist one decision and any reservations in a single transaction."""
        if request.mode not in DIAL_MODES:
            raise ValueError(f"Unsupported pacing mode: {request.mode!r}.")
        if type(request.requested_count) is not int or not 0 <= request.requested_count < 2**31:
            raise ValueError("Requested count must be a nonnegative PostgreSQL integer.")
        if now is not None and now.utcoffset() is None:
            raise ValueError("Approval time must include a time zone.")

        with (psycopg.connect(self._dsn) if connection is None else nullcontext(connection)) as db:
            if db.info.transaction_status != TransactionStatus.IDLE:
                raise ValueError("Approval requires a connection without an open transaction.")
            # After a conflicting insert, the replay SELECT needs a fresh snapshot.
            db.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            return self._approve_in_transaction(db, request, now)

    def _approve_in_transaction(
        self, connection: psycopg.Connection, request: PaceRequest, now: datetime | None,
    ) -> ApprovalDecision:
        with connection.transaction():
            campaign = connection.execute(
                "SELECT mode, enabled, provider FROM campaigns WHERE id = %s FOR SHARE",
                (request.campaign_id,),
            ).fetchone()
            if campaign is None:
                raise ValueError(f"Unknown campaign {request.campaign_id}.")
            mode, enabled, provider = campaign

            # A replay can conflict with either the primary key or the composite foreign-key target.
            claimed = connection.execute(
                """
                INSERT INTO pacing_decisions (id, campaign_id, mode, requested_count, approved_count, reason, inputs)
                VALUES (%s, %s, %s, %s, 0, 'pending', %s)
                ON CONFLICT DO NOTHING RETURNING id
                """,
                (request.id, request.campaign_id, request.mode, request.requested_count, Jsonb(request.inputs)),
            ).fetchone()
            if claimed is None:
                original = connection.execute(
                    """
                    SELECT id, campaign_id, mode, requested_count, approved_count, reason
                    FROM pacing_decisions WHERE id = %s
                    """,
                    (request.id,),
                ).fetchone()
                if original[1:3] != (request.campaign_id, request.mode):
                    raise ValueError("Request ID already belongs to a different campaign or mode.")
                return ApprovalDecision(*original)

            if mode != request.mode:
                raise ValueError(f"Campaign is configured for {mode} mode.")

            approved = 0
            if not enabled:
                reason = "campaign_disabled"
            elif not request.requested_count:
                reason = "no_demand"
            else:
                limit = min(request.requested_count, MAX_APPROVAL_BATCH)
                checked_at = now
                if checked_at is None:
                    checked_at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
                blocked_until = connection.execute(
                    "SELECT blocked_until FROM provider_health WHERE provider = %s", (provider,)
                ).fetchone()[0]
                if blocked_until is not None and blocked_until > checked_at:
                    reason = "provider_unavailable"
                else:
                    approved = self._allocator.reserve_pairs(
                        connection, decision_id=request.id, campaign_id=request.campaign_id,
                        limit=limit, now=checked_at, provider=provider,
                    )
                    if approved < limit:
                        reason = "insufficient_capacity"
                    elif approved < request.requested_count:
                        reason = "batch_limit"
                    else:
                        reason = "approved"

            connection.execute(
                "UPDATE pacing_decisions SET approved_count = %s, reason = %s WHERE id = %s",
                (approved, reason, request.id),
            )
            return ApprovalDecision(
                request.id, request.campaign_id, request.mode, request.requested_count, approved, reason,
            )
