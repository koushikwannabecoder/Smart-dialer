from datetime import datetime

import psycopg

from smartdialer.domain import TERMINAL_CALL_STATES, AgentState, CallState, InvalidTransition, validate_transition
from smartdialer.providers.base import ProviderEvent
from smartdialer.recovery import AttemptContext, change_agent, lock_attempt, record_agent_loss, set_job


def apply_evidence(
    connection: psycopg.Connection,
    ctx: AttemptContext,
    target: CallState,
    *,
    occurred_at: datetime,
    now: datetime,
    accepted_at: datetime | None = None,
    provider_call_id: str | None = None,
    terminal_reason: str | None = None,
) -> None:
    """Apply provider evidence under attempt, job and agent locks."""
    attempt = ctx.attempt
    current = ctx.state
    loss_at = record_agent_loss(connection, ctx, now)
    ended_at = attempt["terminated_at"]
    if ended_at is not None and occurred_at > ended_at:
        return
    if provider_call_id and attempt["provider_call_id"] not in (None, provider_call_id):
        raise ValueError("Provider call ID conflicts with the attempt's existing ID.")

    bridge_possible = loss_at is None or occurred_at < loss_at
    if target == CallState.CONNECTED and not bridge_possible:
        # The callback can still prove provider acceptance after staffing was lost.
        target = CallState.INITIATED
    try:
        validate_transition(current, target)
        state = target
    except InvalidTransition:
        # Duplicate and older events may add facts without regressing state.
        state = current

    if target == CallState.UNKNOWN and state != CallState.UNKNOWN:
        return
    terminal = current not in TERMINAL_CALL_STATES and state in TERMINAL_CALL_STATES
    answered_at = occurred_at if target == CallState.ANSWERED else None
    connected_at = occurred_at if target == CallState.CONNECTED else None
    incident_at = answered_at if answered_at is not None and not bridge_possible else None

    dispatch = attempt["dispatch_status"]
    if accepted_at is not None:
        dispatch = "CONFIRMED"
    elif target == CallState.UNKNOWN:
        dispatch = "UNKNOWN"
    elif terminal and terminal_reason == "provider_rejected":
        dispatch = "REJECTED"
    elif terminal and state == CallState.CANCELLED and dispatch != "CONFIRMED":
        dispatch = "CANCELLED"

    earlier_facts = (
        ("provider_accepted_at", accepted_at), ("answered_at", answered_at), ("connected_at", connected_at),
        ("answered_without_agent_at", incident_at),
    )
    if (state == current and dispatch == attempt["dispatch_status"]
            and provider_call_id in (None, attempt["provider_call_id"])
            and not any(stamp is not None and (attempt[field] is None or stamp < attempt[field])
                        for field, stamp in earlier_facts)):
        return

    connection.execute(
        """
        UPDATE call_attempts SET
            state = %s, dispatch_status = %s, updated_at = %s,
            provider_call_id = coalesce(provider_call_id, %s),
            provider_accepted_at = least(provider_accepted_at, %s), answered_at = least(answered_at, %s),
            connected_at = least(connected_at, %s), answered_without_agent_at = least(answered_without_agent_at, %s),
            terminated_at = coalesce(terminated_at, %s), terminal_reason = coalesce(terminal_reason, %s)
        WHERE id = %s
        """,
        (state, dispatch, now, provider_call_id, accepted_at, answered_at, connected_at, incident_at,
         occurred_at if terminal else None, (terminal_reason or state.lower()) if terminal else None, attempt["id"]),
    )
    attempt["state"] = state

    if state == CallState.CONNECTED and current != state and ctx.agent["state"] == AgentState.DIALING:
        change_agent(connection, ctx, AgentState.CONNECTED, now)
    if not terminal:
        return

    if ctx.agent["state"] in {AgentState.RESERVED, AgentState.DIALING, AgentState.CONNECTED}:
        bridged = attempt["connected_at"] is not None or ctx.agent["state"] == AgentState.CONNECTED
        change_agent(connection, ctx, AgentState.WRAP_UP if bridged else AgentState.AVAILABLE, now)
    retryable_borrower = (state == CallState.CANCELLED and dispatch == "CANCELLED"
                          and attempt["provider_accepted_at"] is None)
    connection.execute(
        "UPDATE borrowers SET status = %s, updated_at = %s WHERE id = %s",
        ("eligible" if retryable_borrower else "done", now, attempt["borrower_id"]),
    )
    set_job(connection, attempt["id"], "DONE", now)


def record_event(dsn: str, event: ProviderEvent, now: datetime) -> None:
    """Commit to the inbox before acknowledging the source."""
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            INSERT INTO provider_events (provider, provider_event_id, attempt_id, event_type, occurred_at, received_at)
            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (provider, provider_event_id) DO NOTHING
            """,
            (event.provider, event.event_id, event.attempt_id, event.event_type, event.occurred_at, now),
        )


def process_one_event(dsn: str, now: datetime) -> bool:
    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT id, provider, attempt_id, event_type, occurred_at FROM provider_events
            WHERE processed_at IS NULL ORDER BY received_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            """
        ).fetchone()
        if row is None:
            return False
        event_id, provider, attempt_id, kind, occurred_at = row
        ctx = lock_attempt(connection, attempt_id)
        error = None
        if ctx.attempt["provider"] != provider or ctx.attempt["dispatch_started_at"] is None or ctx.job is None:
            error = "event has no matching dispatched attempt"
        else:
            target = CallState(kind)
            apply_evidence(connection, ctx, target, occurred_at=occurred_at, now=now, accepted_at=occurred_at)
        connection.execute(
            "UPDATE provider_events SET processed_at = %s, processing_error = %s WHERE id = %s",
            (now, error, event_id),
        )
        return True
