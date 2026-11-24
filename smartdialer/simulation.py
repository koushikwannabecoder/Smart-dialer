from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import blake2b
from itertools import count
from uuid import UUID

import psycopg

from smartdialer.allocator import AGENT_FRESHNESS
from smartdialer.experiment_db import isolated_schema, seed_campaign
from smartdialer.metrics import CallMetrics, read_call_metrics
from smartdialer.pacing import DIAL_MODES, PredictivePacer, ProgressivePacer
from smartdialer.pacing_stats import read_pacing_snapshot
from smartdialer.providers.base import ProviderEventType as Kind, ProviderOutcome, ProviderResult
from smartdialer.providers.mock_fast import StartBehavior, Timeline
from smartdialer.providers.mock_unreliable import MockUnreliableProvider
from smartdialer.safety import SafetyController
from smartdialer.worker import DispatchWorker

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class Profile:
    answer_rate: float
    talk_seconds: int
    setup_seconds: int = 2
    rejection_rate: float = 0.03
    timeout_rate: float = 0.08


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    duration: int
    tick: int
    agents: int
    profile: Profile
    later_profile: Profile | None = None
    outage: tuple[int, int] | None = None
    drop_at: int | None = None
    cancel_outage: tuple[int, int] | None = None


SCENARIOS = {
    "A": Scenario("A", 360, 5, 8, Profile(0.20, 120)),
    "B": Scenario("B", 360, 5, 8, Profile(0.50, 90, setup_seconds=3)),
    "C": Scenario("C", 360, 5, 8, Profile(0.70, 180, setup_seconds=4)),
    "D": Scenario("D", 480, 5, 8, Profile(0.70, 180), later_profile=Profile(0.10, 90, setup_seconds=4)),
    "outage": Scenario("outage", 64, 2, 12, Profile(0.50, 25), outage=(4, 14)),
    "agent_loss": Scenario(
        "agent_loss", 12, 1, 100, Profile(0.70, 20, 8, rejection_rate=0, timeout_rate=0),
        drop_at=2, cancel_outage=(2, 10),
    ),
}


@dataclass(slots=True)
class LogicalClock:
    second: int = 0

    def __call__(self) -> datetime:
        return EPOCH + timedelta(seconds=self.second)


def _draw(seed: int, scenario: str, destination: str, channel: str) -> float:
    digest = blake2b(f"{seed}|{scenario}|{destination}|{channel}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def _in_window(second: int, window: tuple[int, int] | None) -> bool:
    return window is not None and window[0] <= second < window[1]


class ScenarioProvider(MockUnreliableProvider):
    def __init__(self, dsn: str, scenario: Scenario, seed: int, clock: LogicalClock) -> None:
        super().__init__(dsn, clock=clock)
        self.scenario = scenario
        self.seed = seed
        self.clock = clock
        self.timeouts: Counter[str] = Counter()
        self.timed_out_starts: set[str] = set()
        self.start_rejections = 0

    def _call_script(self, key: str, destination: str) -> tuple[StartBehavior, Timeline]:
        if _in_window(self.clock.second, self.scenario.outage):
            return "timeout_before", ()
        profile = self.scenario.profile
        if self.scenario.later_profile is not None and self.clock.second >= self.scenario.duration // 2:
            profile = self.scenario.later_profile

        def draw(channel: str) -> float:
            return _draw(self.seed, self.scenario.name, destination, channel)

        if draw("rejection") < profile.rejection_rate:
            return "reject", ()
        behavior = "timeout_after" if draw("timeout") < profile.timeout_rate else "accept"
        setup = profile.setup_seconds + int(draw("latency") * 2)
        ring = max(1, setup // 2)
        timeline: Timeline = ((Kind.RINGING, ring, setup + 2 if draw("reorder") < 0.25 else ring),)
        if draw("answer") >= profile.answer_rate:
            return behavior, timeline + ((Kind.NO_ANSWER, setup, setup),)
        timeline += ((Kind.ANSWERED, setup, setup),)
        if draw("duplicate") < 0.25:
            timeline += ((Kind.ANSWERED, setup, setup + 1),)
        end = setup + 1 + profile.talk_seconds
        return behavior, timeline + ((Kind.CONNECTED, setup + 1, setup + 1), (Kind.COMPLETED, end, end))

    def start_call(self, attempt_id: UUID, destination: str, key: str) -> ProviderResult:
        result = super().start_call(attempt_id, destination, key)
        if result.outcome == ProviderOutcome.UNKNOWN:
            self.timeouts["start"] += 1
            self.timed_out_starts.add(key)
        elif result.outcome == ProviderOutcome.REJECTED:
            self.start_rejections += 1
        return result

    def lookup(self, key: str) -> ProviderResult:
        if _in_window(self.clock.second, self.scenario.outage):
            self.timeouts["lookup"] += 1
            return ProviderResult(ProviderOutcome.UNKNOWN)
        return super().lookup(key)

    def cancel(self, key: str) -> ProviderResult:
        if _in_window(self.clock.second, self.scenario.outage) or _in_window(self.clock.second, self.scenario.cancel_outage):
            self.timeouts["cancel"] += 1
            return ProviderResult(ProviderOutcome.UNKNOWN)
        return super().cancel(key)


@dataclass(frozen=True, slots=True)
class PacingTick:
    second: int
    staffed: int
    available: int
    requested: int
    approved: int
    reason: str
    strategy: str
    answer_rate: float | None
    inputs: dict[str, object]


@dataclass(frozen=True, slots=True)
class DropReport:
    second: int
    before: int
    lost: int
    remaining: int
    in_flight_at_loss: int
    explicit_observed_at: int | None
    expiry_observed_at: int | None
    approvals_for_lost_agents_after_visibility: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    scenario: str
    mode: str
    seed: int
    duration_seconds: int
    tick_seconds: int
    agents: int
    requested: int
    approved: int
    reduced_decisions: int
    rejected_decisions: int
    safety_reasons: dict[str, int]
    provider_start_rejections: int
    provider_timeouts: dict[str, int]
    attempts_with_start_timeout: int
    events_during_outage: int
    calls: CallMetrics
    drop: DropReport | None
    pacing_trace: tuple[PacingTick, ...]


def _refresh_presence(dsn: str, now: datetime, live: list[UUID], newly_offline: list[UUID]) -> None:
    with psycopg.connect(dsn) as connection:
        if newly_offline:
            connection.execute("UPDATE agents SET state = 'OFFLINE', updated_at = %s WHERE id = ANY(%s)", (now, newly_offline))
        connection.execute("UPDATE agents SET last_seen_at = %s WHERE id = ANY(%s)", (now, live))


def _visible_loss(connection: psycopg.Connection, agents: list[UUID], now: datetime) -> bool:
    return not connection.execute(
        "SELECT 1 FROM agents WHERE id = ANY(%s) AND state <> 'OFFLINE' AND last_seen_at >= %s LIMIT 1",
        (agents, now - AGENT_FRESHNESS),
    ).fetchone()


def run_scenario(dsn: str, scenario: Scenario, mode: str, seed: int) -> SimulationResult:
    if mode not in DIAL_MODES:
        raise ValueError(f"Unknown simulation mode {mode!r}.")
    if scenario.tick < 1 or scenario.duration < 1 or scenario.duration % scenario.tick:
        raise ValueError("Simulation duration must be a positive multiple of the tick size.")
    if scenario.agents < 0:
        raise ValueError("Agent count must be nonnegative.")
    if scenario.drop_at is not None and (scenario.agents < 40 or not 0 < scenario.drop_at < scenario.duration
                                         or scenario.drop_at % scenario.tick):
        raise ValueError("Agent-loss runs need 40 agents and a loss on a tick inside the observation window.")
    with isolated_schema(dsn) as isolated:
        return _run_scenario(isolated, scenario, mode, seed)


def _run_scenario(dsn: str, scenario: Scenario, mode: str, seed: int) -> SimulationResult:
    clock = LogicalClock()
    provider = ScenarioProvider(dsn, scenario, seed, clock)
    campaign_id, agents = seed_campaign(
        dsn, mode=mode, provider=provider.name, agent_count=scenario.agents,
        borrower_count=scenario.agents * (scenario.duration // scenario.tick + 1), now=clock(),
    )
    ids = count(1)
    safety = SafetyController(dsn, new_id=lambda: UUID(int=next(ids)))
    worker = DispatchWorker(dsn, {provider.name: provider}, sources=(provider,), clock=clock)
    pacer = PredictivePacer() if mode == "predictive" else ProgressivePacer()
    explicit, missed = (agents[:20], agents[20:40]) if scenario.drop_at is not None else ([], [])
    live = agents
    dropped_at: datetime | None = None
    explicit_observed = expiry_observed = None
    in_flight = events_during_outage = 0
    trace = []
    # A tick with 100 agents may receive several callbacks per attempt.
    work_limit = max(50, scenario.agents * 6)
    for second in range(0, scenario.duration + 1, scenario.tick):
        clock.second = second
        now = clock()
        newly_offline = []
        if second == scenario.drop_at:
            dropped_at = now
            live = agents[40:]
            newly_offline = explicit
            with psycopg.connect(dsn) as connection:
                in_flight, = connection.execute(
                    """
                    SELECT count(*) FROM call_attempts WHERE agent_id = ANY(%s) AND dispatch_started_at IS NOT NULL
                      AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                    """,
                    (explicit + missed,),
                ).fetchone()
        _refresh_presence(dsn, now, live, newly_offline)
        work = worker.run_once(limit=work_limit)
        if _in_window(second, scenario.outage):
            events_during_outage += work.events
        with psycopg.connect(dsn) as connection:
            connection.read_only = True
            if dropped_at is not None:
                if explicit_observed is None and _visible_loss(connection, explicit, now):
                    explicit_observed = second
                if expiry_observed is None and _visible_loss(connection, missed, now):
                    expiry_observed = second
            if second == scenario.duration:
                break
            snapshot = read_pacing_snapshot(connection, campaign_id, now=now)
        request = pacer.request(snapshot)
        decision = safety.approve(request, now=now)
        trace.append(PacingTick(
            second, snapshot.staffed_agents, snapshot.available_agents, decision.requested_count,
            decision.approved_count, decision.reason, str(request.inputs.get("strategy", mode)),
            request.inputs.get("answer_rate"), request.inputs,
        ))
        if decision.approved_count:
            worker.run_once(limit=work_limit)

    staffed_seconds = scenario.agents * scenario.duration
    departures = {}
    drop = None
    with psycopg.connect(dsn) as connection:
        if dropped_at is not None:
            departures = dict.fromkeys(explicit + missed, dropped_at)
            staffed_seconds -= 40 * (scenario.duration - scenario.drop_at)
            approvals_after_visibility, = connection.execute(
                """
                SELECT count(*) FROM call_attempts WHERE (agent_id = ANY(%s) AND approved_at >= %s)
                    OR (agent_id = ANY(%s) AND approved_at >= %s)
                """,
                (explicit, EPOCH + timedelta(seconds=explicit_observed) if explicit_observed is not None else None,
                 missed, EPOCH + timedelta(seconds=expiry_observed) if expiry_observed is not None else None),
            ).fetchone()
            drop = DropReport(scenario.drop_at, scenario.agents, 40, scenario.agents - 40, in_flight,
                              explicit_observed, expiry_observed, approvals_after_visibility)
        calls = read_call_metrics(connection, campaign_id, EPOCH, clock(), staffed_seconds, departures)
    return SimulationResult(
        scenario.name, mode, seed, scenario.duration, scenario.tick, scenario.agents,
        sum(tick.requested for tick in trace), sum(tick.approved for tick in trace),
        sum(0 < tick.approved < tick.requested for tick in trace),
        sum(tick.requested > 0 and tick.approved == 0 for tick in trace),
        dict(sorted(Counter(tick.reason for tick in trace).items())),
        provider.start_rejections, dict(sorted(provider.timeouts.items())), len(provider.timed_out_starts),
        events_during_outage, calls, drop, tuple(trace),
    )


def describe_result(result: SimulationResult) -> str:
    calls = result.calls
    util = "n/a" if calls.utilization is None else f"{calls.utilization:.1%}"
    timeouts = sum(result.provider_timeouts.values())
    first_clamp = next((tick for tick in result.pacing_trace if tick.approved < tick.requested), None)
    lines = [
        f"{result.scenario:<10} {result.mode:<11} seed={result.seed} t={result.duration_seconds}s "
        f"tick={result.tick_seconds}s agents={result.agents} utilization={util} "
        f"requested/approved={result.requested}/{result.approved} "
        f"dispatch-started/initiated={calls.dispatch_started}/{calls.provider_confirmed_initiated} "
        f"answered/bridged/completed={calls.answered}/{calls.bridged}/{calls.completed} "
        f"provider-rejections/timeouts={result.provider_start_rejections}/{timeouts} "
        f"unresolved/unknown={calls.unresolved_at_end}/{calls.unknown_at_end} "
        f"answered-without-agent={calls.answered_without_agent}",
        f"  safety: {result.safety_reasons}; reduced={result.reduced_decisions}, rejected={result.rejected_decisions}",
        f"  provider timeouts by operation: {result.provider_timeouts}; distinct timed-out starts="
        f"{result.attempts_with_start_timeout}",
    ]
    if first_clamp is not None:
        lines.append(f"  first clamp at t={first_clamp.second}s: {first_clamp.requested} requested, "
                     f"{first_clamp.approved} approved ({first_clamp.reason})")
    if result.drop is not None:
        lines.append(f"  agent loss: {asdict(result.drop)}")
    if result.scenario == "outage":
        lines.append(f"  events applied during blackout: {result.events_during_outage}")
    return "\n".join(lines)
