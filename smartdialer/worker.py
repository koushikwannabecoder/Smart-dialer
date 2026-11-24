from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from smartdialer.domain import TERMINAL_CALL_STATES, AgentState, CallState, validate_transition
from smartdialer.events import apply_evidence, process_one_event, provider_transition, record_event
from smartdialer.providers.base import EventSource, Provider, ProviderEventType, ProviderOutcome, ProviderResult
from smartdialer.recovery import (
    Lease, change_agent, claim_job, lock_attempt, record_agent_loss, release_wrap_up, set_job,
)

POLL_INTERVAL = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class DispatchPlan:
    action: str
    provider: str
    key: str
    destination: str | None = None


@dataclass(frozen=True, slots=True)
class WorkSummary:
    jobs: int
    events: int
    wrapped_up: int


class DispatchWorker:
    def __init__(
        self, dsn: str, providers: Mapping[str, Provider], *, sources: Sequence[EventSource] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dsn = dsn
        self._providers = providers
        self._sources = sources
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._owner = str(uuid4())

    def claim_job(self) -> Lease | None:
        with psycopg.connect(self._dsn) as connection:
            return claim_job(connection, self._owner, self._clock())

    def _plan(self, lease: Lease, *, retry: bool = False) -> DispatchPlan | None:
        with psycopg.connect(self._dsn) as connection:
            ctx = lock_attempt(connection, lease.attempt_id)
            now = self._clock()
            if not ctx.holds_lease(lease, self._owner, now):
                return None
            attempt = ctx.attempt
            if ctx.state in TERMINAL_CALL_STATES or attempt["decision_id"] is None:
                set_job(connection, lease.attempt_id, "DONE", now)
                return None

            provider, key = attempt["provider"], attempt["idempotency_key"]
            if provider not in self._providers:
                set_job(connection, lease.attempt_id, "RECONCILE", now + lease.backoff, "provider unavailable")
                return None
            loss = record_agent_loss(connection, ctx, now)
            if ctx.state != CallState.RESERVED and loss is not None:
                return DispatchPlan("cancel", provider, key)
            if ctx.state != CallState.RESERVED and not retry:
                return DispatchPlan("lookup", provider, key)

            enabled, blocked_until = connection.execute(
                """
                SELECT c.enabled, h.blocked_until FROM campaigns c JOIN provider_health h ON h.provider = %s
                WHERE c.id = %s
                """,
                (provider, attempt["campaign_id"]),
            ).fetchone()
            if retry and (ctx.state not in {CallState.DISPATCHING, CallState.UNKNOWN}
                          or attempt["dispatch_status"] == "CONFIRMED"):
                set_job(connection, lease.attempt_id, "RECONCILE", now + lease.backoff, "known call not found")
                return None
            expected_agent = AgentState.DIALING if retry else AgentState.RESERVED
            if loss is not None or ctx.agent["state"] != expected_agent or not enabled:
                if attempt["dispatch_started_at"] is not None:
                    return DispatchPlan("cancel", provider, key)
                reason = "campaign_disabled" if not enabled else "agent_unavailable"
                apply_evidence(connection, ctx, CallState.CANCELLED, occurred_at=now, now=now, terminal_reason=reason)
                return None
            if blocked_until is not None and blocked_until > now:
                set_job(connection, lease.attempt_id, "RECONCILE", min(blocked_until, now + lease.backoff), "provider cooldown")
                return None

            if ctx.state != CallState.DISPATCHING:
                validate_transition(ctx.state, CallState.DISPATCHING)
            connection.execute(
                """
                UPDATE call_attempts SET state = 'DISPATCHING', dispatch_status = 'STARTED', updated_at = %s,
                    dispatch_started_at = coalesce(dispatch_started_at, %s)
                WHERE id = %s
                """,
                (now, now, lease.attempt_id),
            )
            change_agent(connection, ctx, AgentState.DIALING, now)
            destination = connection.execute(
                "SELECT destination FROM borrowers WHERE id = %s", (attempt["borrower_id"],)
            ).fetchone()[0]
            return DispatchPlan("start", provider, key, destination)

    def dispatch(self, lease: Lease) -> None:
        plan = self._plan(lease)
        if plan is None:
            return
        provider = self._providers[plan.provider]
        if plan.action == "lookup":
            result = provider.lookup(plan.key)
            if result.outcome != ProviderOutcome.ABSENT:
                self._finish(lease, plan, result)
                return
            # Lookup alone does not seal a key against a delayed prior worker.
            plan = self._plan(lease, retry=True)
            if plan is None:
                return
        if plan.action == "cancel":
            result = provider.cancel(plan.key)
        else:
            result = provider.start_call(lease.attempt_id, plan.destination, plan.key)
        self._finish(lease, plan, result)

    def _finish(self, lease: Lease, plan: DispatchPlan, result: ProviderResult) -> None:
        with psycopg.connect(self._dsn) as connection:
            ctx = lock_attempt(connection, lease.attempt_id)
            now = self._clock()
            if not ctx.holds_lease(lease, self._owner, now) or ctx.state in TERMINAL_CALL_STATES:
                return
            unknown = result.outcome in {ProviderOutcome.UNKNOWN, ProviderOutcome.ABSENT}
            if plan.action == "start":
                connection.execute(
                    """
                    UPDATE provider_health SET consecutive_failures = CASE WHEN %s THEN consecutive_failures + 1 ELSE 0 END,
                        blocked_until = CASE WHEN %s AND consecutive_failures >= 2 THEN %s ELSE blocked_until END,
                        updated_at = %s WHERE provider = %s
                    """,
                    (unknown, unknown, now + timedelta(seconds=30), now, plan.provider),
                )
            else:
                connection.execute("UPDATE call_attempts SET last_reconciled_at = %s WHERE id = %s", (now, lease.attempt_id))

            terminal_reason = None
            if unknown:
                target = CallState.UNKNOWN
            elif result.outcome == ProviderOutcome.REJECTED:
                target = CallState.FAILED
                terminal_reason = "provider_rejected"
            elif result.outcome == ProviderOutcome.CANCELLED:
                target = CallState.CANCELLED
            else:
                target, terminal_reason = provider_transition(result.observed_state or ProviderEventType.INITIATED)
            occurred_at = result.occurred_at or now
            accepted_at = result.accepted_at
            if accepted_at is None and result.outcome == ProviderOutcome.ACCEPTED:
                accepted_at = occurred_at
            apply_evidence(
                connection, ctx, target, occurred_at=occurred_at, now=now,
                accepted_at=accepted_at, provider_call_id=result.provider_call_id,
                terminal_reason=terminal_reason,
            )
            if ctx.state not in TERMINAL_CALL_STATES:
                set_job(connection, lease.attempt_id, "RECONCILE", now + (lease.backoff if unknown else POLL_INTERVAL),
                        "provider outcome unknown" if unknown else None)

    def run_once(self, limit: int = 50) -> WorkSummary:
        for source in self._sources:
            for event in source.poll_events(limit):
                record_event(self._dsn, event, self._clock())
                source.ack_event(event.event_id)
        events = 0
        while events < limit and process_one_event(self._dsn, self._clock()):
            events += 1
        jobs = 0
        while jobs < limit and (lease := self.claim_job()) is not None:
            self.dispatch(lease)
            jobs += 1
        with psycopg.connect(self._dsn) as connection:
            wrapped_up = release_wrap_up(connection, self._clock(), limit)
        return WorkSummary(jobs, events, wrapped_up)
