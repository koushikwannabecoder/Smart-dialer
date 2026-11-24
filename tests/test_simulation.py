import psycopg
import pytest

from smartdialer.load_test import run_load_test
from smartdialer.simulation import Profile, Scenario, run_scenario


@pytest.mark.parametrize("mode", ["progressive", "predictive"])
def test_seed_42_reproduces_business_metrics_and_every_decision(test_dsn, mode):
    scenario = Scenario("repeat", duration=30, tick=3, agents=4, profile=Profile(0.5, 6))
    first = run_scenario(test_dsn, scenario, mode, seed=42)
    second = run_scenario(test_dsn, scenario, mode, seed=42)

    assert first == second
    assert first.approved > 0
    assert 0 < first.calls.bridged <= first.calls.answered <= first.calls.provider_confirmed_initiated
    assert first.calls.staffed_agent_seconds == 120
    assert first.calls.utilization == first.calls.connected_agent_seconds / 120
    assert 0 < first.calls.utilization <= 1
    with psycopg.connect(test_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname ~ '^smartdialer_run_[a-f0-9]{32}$'",
        ).fetchone() == (0,)


def test_utilization_is_unavailable_for_zero_staffed_seconds(test_dsn):
    result = run_scenario(test_dsn, Scenario("empty", 4, 2, 0, Profile(0.2, 5)), "predictive", seed=42)

    assert result.calls.utilization is None
    assert result.requested == result.approved == result.calls.dispatch_started == 0


def test_load_workers_exhaust_real_constraint_backed_pairs(test_dsn):
    result = run_load_test(test_dsn, agents=100, borrowers=200, workers=4)

    assert result.backend_connections == 4
    assert result.active_pairs == result.jobs == 100
    assert result.database_errors == result.invariant_violations == ()
    assert result.elapsed_seconds > 0
    assert result.approvals_per_second > 0
