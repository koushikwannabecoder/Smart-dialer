import re
from importlib.resources import files

import psycopg

_MIGRATION_FILENAME = re.compile(r"([0-9]+)_.+\.sql")
_MIGRATION_LOCK = 5182390451765173051  # Stable PostgreSQL advisory lock for this project.


def migrate(dsn: str) -> list[int]:
    """Apply pending migrations and return the versions applied by this run."""
    versions = {}
    for resource in files("smartdialer.migrations").iterdir():
        match = _MIGRATION_FILENAME.fullmatch(resource.name)
        if match and resource.is_file():
            version = int(match[1])
            if version in versions:
                raise ValueError(f"Duplicate migration version {version}.")
            versions[version] = resource

    applied_now = []
    with psycopg.connect(dsn, autocommit=True) as connection:
        # The session holds the lock across the per-migration commits.
        connection.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version integer PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            version for (version,) in connection.execute("SELECT version FROM schema_migrations")
        }
        for version, resource in sorted(versions.items()):
            if version in applied:
                continue
            with connection.transaction():
                connection.execute(resource.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            applied_now.append(version)
    return applied_now
