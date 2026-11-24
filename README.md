# SmartDialer

SmartDialer is a Python and PostgreSQL prototype for progressive outbound dialing with predictive pacing. The chosen architecture puts an independent Safety Controller between both pacing modes and provider dispatch.

**Phase 2 supports progressive approvals.** SmartDialer can request calls, reserve eligible agents and borrowers in PostgreSQL, and queue approved attempts. Jobs remain queued at this phase. Provider dispatch, recovery, predictive pacing, simulation, and load testing come later.

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

On an empty database the first run prints `Applied migrations: 001, 002`. The second run prints `No pending migrations.` An existing Phase 1 database only applies `002`. Existing attempts are preserved without new approvals or jobs. If old data contains duplicate active owners, PostgreSQL rejects the migration until those records are reconciled.

## Run the progressive approval demo

Seed synthetic data and request approvals immediately. Agents are eligible for five seconds after the heartbeat recorded by the seed command. Use a fresh campaign name for each new demo.

```bash
python -m smartdialer.cli seed-demo --campaign demo --agents 5 --borrowers 20 && \
python -m smartdialer.cli approve --campaign demo --mode progressive
```

The approval command prints a decision UUID and `requested 5, approved 5 (approved)`. Each approval reserves one agent and borrower, writes a `RESERVED` attempt with `PENDING` dispatch status, and creates one `READY` job. A job in this state is ready for a future worker. To retry an approval, pass the printed UUID as `--request-id`. The command returns the original counts without creating more jobs.

## Testing

```bash
pytest -q
```

The PostgreSQL tests create and drop their own schemas inside `TEST_DATABASE_URL`. Set it to a disposable database to run the full suite, including the worker races and rollback checks in `tests/test_safety.py` and `tests/test_allocation_concurrency.py`. If it is unset, pytest skips the database tests.

## Design documentation

- [Architecture](docs/architecture.md)
- [Agent state machine](docs/agent-state-machine.md)
- [Call state machine](docs/call-state-machine.md)
- [Architecture decision record](docs/architecture-decision-record.md)
