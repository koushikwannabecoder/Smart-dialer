# SmartDialer

SmartDialer is a Python and PostgreSQL prototype for progressive outbound dialing with predictive pacing. The chosen architecture puts an independent Safety Controller between both pacing modes and provider dispatch.

**Phase 4 adds predictive pacing.** Both modes reserve agents and borrowers through the same Safety Controller. The worker dispatches approved jobs to two mock providers, applies their events, and recovers ambiguous outcomes. Seeded simulation and load testing come later.

## Prerequisites and installation

- Python 3.11 or later.
- A running PostgreSQL server. The migrations are tested on PostgreSQL 16.
- A PostgreSQL role that can create tables in the development database and schemas in a separate disposable test database.

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Database setup and configuration

Create two databases with your PostgreSQL tools. The following example uses the `createdb` client and a role with database creation rights. Replace `YOUR_POSTGRES_USER` with your local role.

```bash
createdb -h localhost -U YOUR_POSTGRES_USER smartdialer
createdb -h localhost -U YOUR_POSTGRES_USER smartdialer_test
```

Export connection URLs for the two databases. Replace `USER` and `PASSWORD` with your PostgreSQL credentials and adjust the host and port if needed. `.env.example` lists the settings. The CLI reads the shell environment directly.

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/smartdialer'
export TEST_DATABASE_URL='postgresql://USER:PASSWORD@localhost:5432/smartdialer_test'
python -m smartdialer.cli migrate
python -m smartdialer.cli migrate
```

On an empty database the first run prints `Applied migrations: 001, 002, 003, 004`. The second run prints `No pending migrations.` A Phase 3 database applies only `004`, which adds an index for recent outcomes and lets the provider inbox record `NO_ANSWER` explicitly.

## Run a progressive call demo

Use a fresh campaign name for each demo. Seed, approve, and start the worker together because a demo agent's initial heartbeat is fresh for five seconds. The CLI supports `mock_fast` for quick, ordered callbacks and `mock_unreliable` for slower events, duplicate answers, late ringing, and occasional start timeouts.

```bash
python -m smartdialer.cli seed-demo --campaign demo --agents 5 --borrowers 20 --provider mock_fast && \
python -m smartdialer.cli approve --campaign demo --mode progressive && \
python -m smartdialer.cli worker --once

for tick in 1 2 3 4; do
    sleep 1
    python -m smartdialer.cli worker --once
done
```

The approval command prints a decision UUID and `requested 5, approved 5 (approved)`. Each approval creates one `RESERVED` attempt and one `READY` job. Worker output reports processed jobs, applied events, and released agent wrap-ups. The mock fast run ends with five `COMPLETED` attempts and five `DONE` jobs. Pass the printed decision UUID as `--request-id` to replay the approval without creating more jobs.

## Run predictive pacing

```bash
python -m smartdialer.cli seed-demo --campaign forecast --agents 5 --borrowers 20 --mode predictive && \
python -m smartdialer.cli approve --campaign forecast --mode predictive
```

With no call history, this demo uses progressive demand and reports five requested and five approved. The campaign's configured mode is the default for `approve`. To run dispatch, invoke `worker --once` immediately and continue running it as in the progressive demo.

Predictive mode uses up to 50 known outcomes from the selected campaign and provider over the past 30 minutes. It needs at least 10 samples. Answer or bridge evidence counts as an answer. An explicit `NO_ANSWER` counts as a miss. Provider failures, implicit cancellations without answer evidence, and unknown attempts stay out of that rate.

The forecast targets 85% of freshly staffed agents, rounded up, five seconds ahead. It subtracts current connections expected to remain, expected answers from pending setups, and one potential answer per unknown attempt. It divides any remaining demand by the observed answer rate with a 20% floor, rounds up, and caps the request at 50. With 20 staffed agents, eight connected, a mean talk time of 40 seconds, four pending setups, a 50% answer rate, and no unknowns, the target is 17. One connection is forecast to finish and two pending setups are expected to answer, so the estimate is `8 - 1 + 2 = 9`. The predictor requests `ceil((17 - 9) / 0.5) = 16`. With only eight free agents, the controller approves eight.

The controller rechecks current capacity and provider health in PostgreSQL. Predictive demand can change batch sizes within that limit. It can match progressive utilization when progressive dialing already fills every safe slot. Inspect the stored calculation and dispatch progress with `psql`:

```bash
psql "$DATABASE_URL" -c "SELECT d.mode, d.requested_count, d.approved_count, d.reason, d.inputs,
    (SELECT count(*) FROM call_attempts c WHERE c.decision_id = d.id AND c.dispatch_started_at IS NOT NULL)
    AS dispatch_started_count FROM pacing_decisions d ORDER BY d.created_at DESC LIMIT 5;"
```

## Inspect and recover work

The worker processes bounded work and exits. Run `worker --once` again to process due events, retry reconciliation, or reclaim a lease after a worker stops. Leases expire after five seconds. A timeout preserves ownership and causes a same-key lookup before any possible retry. Three consecutive start timeouts pause that provider's new approvals and starts for 30 seconds. Existing events and reconciliation continue during cooldown.

Use `psql` to inspect an attempt alongside its worker job:

```bash
psql "$DATABASE_URL" -c 'SELECT c.id, c.provider, c.state, c.dispatch_status,
    c.provider_call_id, c.answered_without_agent_at, j.state AS job_state, j.last_error
    FROM call_attempts c JOIN dispatch_jobs j ON j.attempt_id = c.id
    ORDER BY c.created_at DESC LIMIT 20;'
```

An unresolved attempt can continue to hold an agent and borrower if the provider cannot confirm an outcome. The mocks use PostgreSQL as a separate provider ledger and place no real calls. Demo agents have an initial heartbeat only. Long manual pauses can cause them to go offline.

## Testing

```bash
pytest -q
```

The PostgreSQL tests create and drop their own schemas inside `TEST_DATABASE_URL`. Set it to a disposable database to run the full suite. `tests/test_recovery.py` includes an actual subprocess exit after provider acceptance. `tests/test_events.py` covers duplicates, reordered events, and replay around `ANSWERED`. If the URL is unset, pytest skips the database tests.

## Design documentation

- [Architecture](docs/architecture.md)
- [Agent state machine](docs/agent-state-machine.md)
- [Call state machine](docs/call-state-machine.md)
- [Architecture decision record](docs/architecture-decision-record.md)
