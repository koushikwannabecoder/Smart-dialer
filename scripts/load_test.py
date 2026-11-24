import argparse
import json
import os
from dataclasses import asdict

import psycopg

from smartdialer.load_test import run_load_test


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure concurrent SmartDialer allocation on PostgreSQL.")
    parser.add_argument("--agents", type=int, default=100)
    parser.add_argument("--borrowers", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    dsn = os.environ.get("LOAD_TEST_DATABASE_URL")
    if not dsn:
        parser.error("Set LOAD_TEST_DATABASE_URL to a disposable PostgreSQL database.")
    try:
        result = run_load_test(dsn, agents=args.agents, borrowers=args.borrowers, workers=args.workers)
    except (psycopg.Error, ValueError) as error:
        parser.exit(1, f"load test failed: {error}\n")
    print(json.dumps(asdict(result), indent=2))
    return int(bool(result.database_errors or result.invariant_violations))


if __name__ == "__main__":
    raise SystemExit(main())
