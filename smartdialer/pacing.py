from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from math import ceil, floor
from uuid import UUID, uuid4

DIAL_MODES = ("progressive", "predictive")
HISTORY_SIZE = 50
HISTORY_WINDOW_SECONDS = 30 * 60
MIN_OUTCOMES = 10
HORIZON_SECONDS = 5
TARGET_UTILIZATION = Fraction(85, 100)
MIN_ANSWER_RATE = Fraction(1, 5)
MAX_PREDICTIVE_REQUEST = 50


@dataclass(frozen=True, slots=True)
class PacingSnapshot:
    campaign_id: UUID
    available_agents: int
    staffed_agents: int = 0
    connected_calls: int = 0
    pending_setups: int = 0
    unknown_attempts: int = 0
    known_answers: int = 0
    known_no_answers: int = 0
    mean_talk_seconds: float | None = None
    provider_available: bool = True
    provider: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PaceRequest:
    id: UUID
    campaign_id: UUID
    mode: str
    requested_count: int
    inputs: dict[str, object] = field(default_factory=dict)


class ProgressivePacer:
    def request(self, snapshot: PacingSnapshot, *, request_id: UUID | None = None) -> PaceRequest:
        """Request one call per available agent in the advisory snapshot."""
        return PaceRequest(
            id=request_id if request_id is not None else uuid4(),
            campaign_id=snapshot.campaign_id,
            mode="progressive",
            requested_count=snapshot.available_agents,
            inputs={"available_agents": snapshot.available_agents},
        )


class PredictivePacer:
    def request(self, snapshot: PacingSnapshot, *, request_id: UUID | None = None) -> PaceRequest:
        """Forecast demand from a snapshot. Only the Safety Controller can approve it."""
        samples = snapshot.known_answers + snapshot.known_no_answers
        inputs: dict[str, object] = {
            "available_agents": snapshot.available_agents,
            "staffed_agents": snapshot.staffed_agents,
            "connected_calls": snapshot.connected_calls,
            "pending_setups": snapshot.pending_setups,
            "unknown_attempts": snapshot.unknown_attempts,
            "known_answers": snapshot.known_answers,
            "known_no_answers": snapshot.known_no_answers,
            "sample_count": samples,
            "mean_talk_seconds": snapshot.mean_talk_seconds,
            "provider": snapshot.provider,
            "provider_available": snapshot.provider_available,
            "observed_at": snapshot.observed_at.isoformat() if snapshot.observed_at else None,
            "history_size": HISTORY_SIZE,
            "history_window_seconds": HISTORY_WINDOW_SECONDS,
            "minimum_outcomes": MIN_OUTCOMES,
            "horizon_seconds": HORIZON_SECONDS,
            "target_utilization": float(TARGET_UTILIZATION),
            "answer_rate_floor": float(MIN_ANSWER_RATE),
            "request_cap": MAX_PREDICTIVE_REQUEST,
        }
        if samples < MIN_OUTCOMES:
            uncapped = snapshot.available_agents
            inputs.update(strategy="progressive_fallback", fallback_reason="insufficient_recent_outcomes")
        else:
            answer_rate = Fraction(snapshot.known_answers, samples)
            divisor = max(answer_rate, MIN_ANSWER_RATE)
            target = ceil(snapshot.staffed_agents * TARGET_UTILIZATION)
            mean_talk = snapshot.mean_talk_seconds
            releases = 0
            if mean_talk is not None and mean_talk > 0:
                release_fraction = min(1, Fraction(HORIZON_SECONDS) / Fraction(str(mean_talk)))
                releases = floor(snapshot.connected_calls * release_fraction)
            pending_answers = snapshot.pending_setups * answer_rate
            connected_remaining = snapshot.connected_calls - releases
            expected_existing = connected_remaining + pending_answers + snapshot.unknown_attempts
            expected_needed = max(0, target - expected_existing)
            uncapped = ceil(expected_needed / divisor)
            inputs.update(
                strategy="predictive", answer_rate=float(answer_rate), answer_rate_divisor=float(divisor),
                target_connected=target, expected_releases=releases, connected_remaining=connected_remaining,
                expected_pending_answers=float(pending_answers), expected_unknown_answers=snapshot.unknown_attempts,
                expected_existing_connections=float(expected_existing), expected_answers_needed=float(expected_needed),
            )
        requested = min(MAX_PREDICTIVE_REQUEST, max(0, uncapped))
        inputs.update(uncapped_requested_count=uncapped, requested_count=requested)
        return PaceRequest(request_id or uuid4(), snapshot.campaign_id, "predictive", requested, inputs)
