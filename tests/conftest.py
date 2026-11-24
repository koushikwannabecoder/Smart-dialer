import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


@pytest.fixture
def test_dsn():
    """Isolate each integration test in its own schema."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests.")

    schema_name = f"smartdialer_test_{uuid4().hex}"
    schema = sql.Identifier(schema_name)
    options = conninfo_to_dict(dsn).get("options", "")
    isolated_dsn = make_conninfo(dsn, options=f"{options} -c search_path={schema_name}")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            yield isolated_dsn
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
