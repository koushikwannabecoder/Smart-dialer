import argparse

import psycopg

from smartdialer.config import database_url
from smartdialer.db import migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartdialer", description="SmartDialer prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending PostgreSQL migrations")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        try:
            versions = migrate(database_url())
        except (psycopg.Error, ValueError) as error:
            parser.exit(1, f"Migration failed: {error}\n")
        if versions:
            print("Applied migrations: " + ", ".join(f"{version:03d}" for version in versions))
        else:
            print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
