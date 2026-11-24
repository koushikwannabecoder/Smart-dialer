import argparse
from uuid import UUID, uuid4

import psycopg

from smartdialer.allocator import count_available_agents
from smartdialer.config import database_url
from smartdialer.db import migrate
from smartdialer.pacing import PacingSnapshot, ProgressivePacer
from smartdialer.providers.mock_fast import FAST_PROVIDER, MockFastProvider
from smartdialer.providers.mock_unreliable import UNRELIABLE_PROVIDER, MockUnreliableProvider
from smartdialer.safety import SafetyController
from smartdialer.worker import DispatchWorker


def _nonnegative_count(value: str) -> int:
    count = int(value)
    if count < 0:
        raise argparse.ArgumentTypeError("count must be nonnegative")
    return count


def _request_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("request ID must be a UUID") from error


def _seed_demo(dsn: str, name: str, agents: int, borrowers: int, provider: str) -> UUID:
    if not name.strip():
        raise ValueError("Campaign name must not be empty.")
    campaign_id = uuid4()
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        if cursor.execute("SELECT 1 FROM campaigns WHERE name = %s LIMIT 1", (name,)).fetchone():
            raise ValueError(f"Campaign {name!r} already exists. Use a fresh demo name.")
        cursor.execute(
            "INSERT INTO campaigns (id, name, mode, provider) VALUES (%s, %s, 'progressive', %s)",
            (campaign_id, name, provider),
        )
        cursor.executemany(
            "INSERT INTO borrowers (id, campaign_id, destination, status) VALUES (%s, %s, %s, 'eligible')",
            ((uuid4(), campaign_id, f"demo-{index + 1}") for index in range(borrowers)),
        )
        cursor.executemany(
            "INSERT INTO agents (id, campaign_id, state, last_seen_at) VALUES (%s, %s, 'AVAILABLE', clock_timestamp())",
            ((uuid4(), campaign_id) for _ in range(agents)),
        )
    return campaign_id


def _read_snapshot(dsn: str, name: str) -> PacingSnapshot:
    with psycopg.connect(dsn) as connection:
        campaigns = connection.execute(
            "SELECT id FROM campaigns WHERE name = %s LIMIT 2", (name,)
        ).fetchall()
        if not campaigns:
            raise ValueError(f"Unknown campaign {name!r}.")
        if len(campaigns) > 1:
            raise ValueError(f"Campaign name {name!r} is ambiguous.")
        (campaign_id,), = campaigns
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        return PacingSnapshot(campaign_id, count_available_agents(connection, campaign_id, now))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartdialer", description="SmartDialer prototype")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply pending PostgreSQL migrations")
    seed = commands.add_parser("seed-demo", help="create a progressive campaign with synthetic data")
    seed.add_argument("--campaign", required=True)
    seed.add_argument("--agents", required=True, type=_nonnegative_count)
    seed.add_argument("--borrowers", required=True, type=_nonnegative_count)
    seed.add_argument("--provider", choices=[FAST_PROVIDER, UNRELIABLE_PROVIDER], default=FAST_PROVIDER)
    approve = commands.add_parser("approve", help="request progressive approvals")
    approve.add_argument("--campaign", required=True)
    approve.add_argument("--mode", choices=["progressive"], default="progressive")
    approve.add_argument("--request-id", type=_request_id, help="UUID to reuse when retrying a request")
    worker = commands.add_parser("worker", help="process queued jobs, provider events, and recovery")
    worker.add_argument("--once", action="store_true", required=True, help="process bounded work and exit")
    args = parser.parse_args(argv)

    try:
        dsn = database_url()
        if args.command == "migrate":
            versions = migrate(dsn)
            if versions:
                print("Applied migrations: " + ", ".join(f"{version:03d}" for version in versions))
            else:
                print("No pending migrations.")
        elif args.command == "seed-demo":
            campaign_id = _seed_demo(dsn, args.campaign, args.agents, args.borrowers, args.provider)
            print(f"Created campaign {args.campaign!r} ({campaign_id}).")
        elif args.command == "approve":
            snapshot = _read_snapshot(dsn, args.campaign)
            request = ProgressivePacer().request(snapshot, request_id=args.request_id)
            decision = SafetyController(dsn).approve(request)
            print(
                f"Decision {decision.id}: requested {decision.requested_count}, "
                f"approved {decision.approved_count} ({decision.reason})."
            )
        else:
            fast, unreliable = MockFastProvider(dsn), MockUnreliableProvider(dsn)
            summary = DispatchWorker(dsn, {fast.name: fast, unreliable.name: unreliable}, sources=(fast, unreliable)).run_once()
            print(f"Jobs processed: {summary.jobs}; events applied: {summary.events}; wrap-ups released: {summary.wrapped_up}.")
    except (psycopg.Error, ValueError) as error:
        parser.exit(1, f"{args.command} failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
