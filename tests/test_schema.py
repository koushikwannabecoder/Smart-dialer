from uuid import uuid4

import psycopg
import pytest

from smartdialer import db


@pytest.fixture
def migrated_connection(test_dsn):
    db.migrate(test_dsn)
    with psycopg.connect(test_dsn, autocommit=True) as connection:
        yield connection


def insert_campaign(connection):
    campaign_id = uuid4()
    connection.execute(
        "INSERT INTO campaigns (id, name, mode) VALUES (%s, 'Campaign', 'progressive')",
        (campaign_id,),
    )
    return campaign_id


def insert_agent(connection, campaign_id, state="AVAILABLE"):
    agent_id = uuid4()
    connection.execute(
        "INSERT INTO agents (id, campaign_id, state) VALUES (%s, %s, %s)",
        (agent_id, campaign_id, state),
    )
    return agent_id


def insert_borrower(connection, campaign_id, status="eligible"):
    borrower_id = uuid4()
    connection.execute(
        "INSERT INTO borrowers (id, campaign_id, destination, status) VALUES (%s, %s, %s, %s)",
        (borrower_id, campaign_id, "+12025550101", status),
    )
    return borrower_id


def insert_attempt(connection, campaign_id, agent_id, borrower_id, state="RESERVED"):
    return connection.execute(
        """
        INSERT INTO call_attempts (id, campaign_id, agent_id, borrower_id, state)
        VALUES (%s, %s, %s, %s, %s) RETURNING state, created_at
        """,
        (uuid4(), campaign_id, agent_id, borrower_id, state),
    ).fetchone()


def test_clean_migration_and_repeat(test_dsn):
    assert db.migrate(test_dsn) == [1, 2, 3]
    assert db.migrate(test_dsn) == []
    with psycopg.connect(test_dsn) as connection:
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,)]
        campaign_id = insert_campaign(connection)
        agent_id = insert_agent(connection, campaign_id)
        borrower_id = insert_borrower(connection, campaign_id)
        state, created_at = insert_attempt(connection, campaign_id, agent_id, borrower_id, "UNKNOWN")
        assert state == "UNKNOWN"
        assert created_at.utcoffset() is not None


def test_upgrade_preserves_phase_one_attempt_without_queuing_it(test_dsn, tmp_path, monkeypatch):
    initial = db.files("smartdialer.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
    (tmp_path / "001_initial.sql").write_text(initial, encoding="utf-8")
    with monkeypatch.context() as migration_resources:
        migration_resources.setattr(db, "files", lambda _: tmp_path)
        assert db.migrate(test_dsn) == [1]

    with psycopg.connect(test_dsn) as connection:
        campaign_id = insert_campaign(connection)
        agent_id = insert_agent(connection, campaign_id)
        borrower_id = insert_borrower(connection, campaign_id)
        insert_attempt(connection, campaign_id, agent_id, borrower_id, "UNKNOWN")

    assert db.migrate(test_dsn) == [2, 3]
    with psycopg.connect(test_dsn) as connection:
        assert connection.execute(
            "SELECT agent_id, borrower_id, state, decision_id, dispatch_status FROM call_attempts"
        ).fetchall() == [(agent_id, borrower_id, "UNKNOWN", None, None)]
        assert connection.execute("SELECT count(*) FROM dispatch_jobs").fetchone() == (0,)


def test_failed_migration_rolls_back_without_undoing_previous_version(test_dsn, tmp_path, monkeypatch):
    initial = db.files("smartdialer.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
    (tmp_path / "002_broken.sql").write_text(
        """
        CREATE TABLE rollback_probe (id uuid REFERENCES campaigns (id));
        INSERT INTO schema_migrations (version) VALUES (2);
        """,
        encoding="utf-8",
    )
    (tmp_path / "001_initial.sql").write_text(initial, encoding="utf-8")
    monkeypatch.setattr(db, "files", lambda _: tmp_path)

    # The runner's own ledger insert fails after the migration SQL succeeds.
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.migrate(test_dsn)
    with psycopg.connect(test_dsn) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert connection.execute("SELECT to_regclass('rollback_probe')").fetchone() == (None,)


@pytest.mark.parametrize("wrong_owner", ["agent", "borrower"])
def test_attempt_cannot_reference_an_owner_from_another_campaign(migrated_connection, wrong_owner):
    connection = migrated_connection
    campaign_id = insert_campaign(connection)
    other_campaign = insert_campaign(connection)
    agent_id = insert_agent(connection, other_campaign if wrong_owner == "agent" else campaign_id)
    borrower_id = insert_borrower(connection, other_campaign if wrong_owner == "borrower" else campaign_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_attempt(connection, campaign_id, agent_id, borrower_id)


def test_invalid_states_and_modes_are_rejected(migrated_connection):
    connection = migrated_connection
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "INSERT INTO campaigns (id, name, mode) VALUES (%s, 'Campaign', 'invalid')", (uuid4(),)
        )
    campaign_id = insert_campaign(connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_agent(connection, campaign_id, state="invalid")
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_borrower(connection, campaign_id, status="invalid")
    agent_id = insert_agent(connection, campaign_id)
    borrower_id = insert_borrower(connection, campaign_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_attempt(connection, campaign_id, agent_id, borrower_id, state="invalid")


def test_destination_is_unique_within_a_campaign(migrated_connection):
    connection = migrated_connection
    campaign_id = insert_campaign(connection)
    insert_borrower(connection, campaign_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_borrower(connection, campaign_id)
    insert_borrower(connection, insert_campaign(connection))
