from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class PacingSnapshot:
    campaign_id: UUID
    available_agents: int


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
