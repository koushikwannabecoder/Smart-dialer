# SmartDialer

SmartDialer is a Python and PostgreSQL prototype for progressive outbound dialing with predictive pacing. The chosen architecture puts an independent Safety Controller between both pacing modes and provider dispatch.

**Phase 1 is the foundation.** The migration CLI, database schema, state rules, and provider interfaces are available. Dialing, reservations, providers, worker recovery, simulation, and load testing are planned for later phases.

## Prerequisites and installation

- Python 3.11 or later.
- A running PostgreSQL server. The initial migration is tested on PostgreSQL 16.
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

On an empty database the first run prints `Applied migrations: 001`. The second run prints `No pending migrations.`

## Testing

```bash
pytest -q tests/test_domain.py tests/test_schema.py
```

The PostgreSQL tests create and drop their own schemas inside `TEST_DATABASE_URL`. Set it to a disposable database to run the full Phase 1 checks. If it is unset, pytest skips the database tests.

## Design documentation

- [Architecture](docs/architecture.md)
- [Agent state machine](docs/agent-state-machine.md)
- [Call state machine](docs/call-state-machine.md)
- [Architecture decision record](docs/architecture-decision-record.md)
