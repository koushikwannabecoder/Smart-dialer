from dataclasses import replace
from uuid import uuid4

from smartdialer.pacing import PacingSnapshot, PredictivePacer


def test_predictive_request_explains_worked_example():
    snapshot = PacingSnapshot(
        uuid4(), 8, staffed_agents=20, connected_calls=8, pending_setups=4,
        known_answers=5, known_no_answers=5, mean_talk_seconds=40,
    )
    request_id = uuid4()
    request = PredictivePacer().request(snapshot, request_id=request_id)

    assert (request.id, request.mode, request.requested_count) == (request_id, "predictive", 16)
    assert request.inputs["target_connected"] == 17
    assert request.inputs["expected_releases"] == 1
    assert request.inputs["connected_remaining"] == 7
    assert request.inputs["expected_pending_answers"] == 2
    assert request.inputs["expected_existing_connections"] == 9
    assert request.inputs["expected_answers_needed"] == 8
    assert request.inputs["answer_rate_divisor"] == 0.5


def test_cold_start_uses_progressive_demand_inside_request_cap():
    snapshot = PacingSnapshot(uuid4(), 12, staffed_agents=12, known_answers=9)
    request = PredictivePacer().request(snapshot)

    assert request.requested_count == 12
    assert request.inputs["strategy"] == "progressive_fallback"
    assert request.inputs["fallback_reason"] == "insufficient_recent_outcomes"
    assert PredictivePacer().request(replace(snapshot, available_agents=100)).requested_count == 50


def test_unknowns_are_counted_in_full_and_missing_talk_times_predict_no_releases():
    snapshot = PacingSnapshot(
        uuid4(), 0, staffed_agents=10, connected_calls=7, pending_setups=2,
        unknown_attempts=1, known_answers=5, known_no_answers=5,
    )
    request = PredictivePacer().request(snapshot)

    assert request.inputs["expected_releases"] == 0
    assert request.inputs["expected_unknown_answers"] == 1
    assert request.inputs["expected_existing_connections"] == 9
    assert request.requested_count == 0


def test_answer_rate_floor_limits_unrounded_demand_then_request_cap_applies():
    snapshot = PacingSnapshot(uuid4(), 100, staffed_agents=100, pending_setups=10, known_no_answers=10)
    request = PredictivePacer().request(snapshot)

    assert request.inputs["answer_rate"] == 0
    assert request.inputs["expected_pending_answers"] == 0
    assert request.inputs["answer_rate_divisor"] == 0.2
    assert request.inputs["uncapped_requested_count"] == 425
    assert request.requested_count == 50
