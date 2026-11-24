from smartdialer.simulation import SCENARIOS, run_scenario


def test_outage_blocks_new_starts_but_keeps_delivering_existing_events(test_dsn):
    result = run_scenario(test_dsn, SCENARIOS["outage"], "predictive", seed=42)

    assert result.provider_timeouts["start"] >= 3
    assert result.provider_timeouts["lookup"] > 0
    assert result.safety_reasons["provider_unavailable"] > 0
    assert result.events_during_outage > 0
    assert all(tick.approved == 0 for tick in result.pacing_trace if tick.reason == "provider_unavailable")
    rejected_at = next(tick.second for tick in result.pacing_trace if tick.reason == "provider_unavailable")
    assert any(tick.approved > 0 for tick in result.pacing_trace if tick.second > rejected_at)


def test_hundred_to_sixty_agents_disappear_and_expiry_has_separate_visibility(test_dsn):
    result = run_scenario(test_dsn, SCENARIOS["agent_loss"], "progressive", seed=42)

    drop = result.drop
    assert drop is not None
    assert (drop.before, drop.lost, drop.remaining) == (100, 40, 60)
    assert drop.in_flight_at_loss == 40
    assert drop.explicit_observed_at == drop.second == 2
    assert drop.expiry_observed_at == 7
    assert drop.approvals_for_lost_agents_after_visibility == 0
    ticks = {tick.second: tick for tick in result.pacing_trace}
    assert ticks[1].staffed == 100 and ticks[2].staffed == 80 and ticks[7].staffed == 60
    assert result.calls.staffed_agent_seconds == 800
    assert result.provider_timeouts["cancel"] > 0
    assert result.calls.answered_without_agent >= result.calls.observed_answered_without_agent > 0
