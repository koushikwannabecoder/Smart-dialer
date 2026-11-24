from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Barrier, Event, Lock
from time import monotonic, perf_counter
from uuid import uuid4

import psycopg

from smartdialer.allocator import DEFAULT_PROVIDER
from smartdialer.experiment_db import isolated_schema, seed_campaign
from smartdialer.pacing import PaceRequest
from smartdialer.safety import MAX_APPROVAL_BATCH, SafetyController


@dataclass(frozen=True, slots=True)
class LoadResult:
    agents: int
    borrowers: int
    workers: int
    backend_connections: int
    batch_size: int
    decisions: int
    zero_approvals: int
    active_pairs: int
    jobs: int
    elapsed_seconds: float
    approvals_per_second: float
    database_errors: tuple[str, ...]
    invariant_violations: tuple[str, ...]


def run_load_test(dsn: str, *, agents: int, borrowers: int, workers: int) -> LoadResult:
    if min(agents, borrowers, workers) < 1:
        raise ValueError("Agent, borrower, and worker counts must be positive.")
    now = datetime.now(timezone.utc)
    target = min(agents, borrowers)
    batch = min(MAX_APPROVAL_BATCH, (target + workers - 1) // workers)
    with isolated_schema(dsn) as isolated:
        campaign_id, _ = seed_campaign(
            isolated, mode="progressive", provider=DEFAULT_PROVIDER, agent_count=agents, borrower_count=borrowers, now=now,
        )
        barrier = Barrier(workers, timeout=15)
        stop = Event()
        counter_lock = Lock()
        pids: set[int] = set()
        errors: list[str] = []
        approved = 0
        decisions = 0
        zero_approvals = 0
        started = perf_counter()
        deadline = monotonic() + 30

        def allocate() -> None:
            nonlocal approved, decisions, zero_approvals
            controller = SafetyController(isolated)
            try:
                with psycopg.connect(isolated, connect_timeout=5) as connection:
                    with counter_lock:
                        pids.add(connection.info.backend_pid)
                    barrier.wait()
                    while not stop.is_set() and monotonic() < deadline:
                        request = PaceRequest(uuid4(), campaign_id, "progressive", batch)
                        decision = controller.approve(request, now=now, connection=connection)
                        with counter_lock:
                            decisions += 1
                            approved += decision.approved_count
                            zero_approvals += int(decision.approved_count == 0)
                            if approved >= target:
                                stop.set()
                        if decision.approved_count == 0:
                            # SKIP LOCKED can return zero while a peer still owns candidate locks.
                            stop.wait(0.005)
            except Exception as error:
                with counter_lock:
                    detail = str(error).partition("\n")[0]
                    errors.append(f"{type(error).__name__}: {detail}" if detail else type(error).__name__)
                stop.set()
                barrier.abort()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            tasks = [pool.submit(allocate) for _ in range(workers)]
            for task in tasks:
                task.result()
        elapsed = perf_counter() - started

        with psycopg.connect(isolated) as connection:
            pairs, distinct_agents, distinct_borrowers, linked_jobs = connection.execute(
                """
                SELECT count(*), count(DISTINCT c.agent_id), count(DISTINCT c.borrower_id), count(j.id)
                FROM call_attempts c
                JOIN agents a ON a.id = c.agent_id AND a.state = 'RESERVED'
                JOIN borrowers b ON b.id = c.borrower_id AND b.status = 'active'
                JOIN pacing_decisions d ON d.id = c.decision_id
                LEFT JOIN dispatch_jobs j ON j.attempt_id = c.id AND j.state = 'READY'
                WHERE c.state = 'RESERVED' AND c.dispatch_status = 'PENDING'
                """
            ).fetchone()
            attempts, jobs, reserved_agents, active_borrowers, mismatched = connection.execute(
                """
                SELECT (SELECT count(*) FROM call_attempts), (SELECT count(*) FROM dispatch_jobs),
                       (SELECT count(*) FROM agents WHERE state = 'RESERVED'),
                       (SELECT count(*) FROM borrowers WHERE status = 'active'),
                       (SELECT count(*) FROM pacing_decisions d
                        WHERE d.approved_count <> (SELECT count(*) FROM call_attempts c WHERE c.decision_id = d.id))
                """
            ).fetchone()
        checks = (
            (pairs == approved == target, f"expected {target} active pairs, found {pairs}; workers counted {approved}"),
            (distinct_agents == pairs, "duplicate active agent ownership"),
            (distinct_borrowers == pairs, "duplicate active borrower ownership"),
            (attempts == pairs == linked_jobs == jobs, "attempts or jobs lack their required links or states"),
            (reserved_agents == active_borrowers == pairs, "reserved agents or active borrowers lack an attempt"),
            (mismatched == 0, "stored approval count does not match attempts"),
            (len(pids) == workers, "workers did not use distinct PostgreSQL connections"),
        )
        return LoadResult(
            agents, borrowers, workers, len(pids), batch, decisions, zero_approvals, pairs, jobs,
            elapsed, pairs / elapsed, tuple(sorted(errors)), tuple(message for valid, message in checks if not valid),
        )
