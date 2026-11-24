from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class ProviderEventType(StrEnum):
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    outcome: ProviderOutcome
    provider_call_id: str | None = None
    observed_state: ProviderEventType | None = None
    occurred_at: datetime | None = None
    accepted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    provider: str
    event_id: str
    attempt_id: UUID
    event_type: ProviderEventType
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.occurred_at.utcoffset() is None:
            raise ValueError("Provider event occurrence time must include a time zone.")


class Provider(Protocol):
    def start_call(self, attempt_id: UUID, destination: str, key: str) -> ProviderResult:
        """Repeated starts with the same key refer to one call. A timeout is UNKNOWN."""
        ...

    def lookup(self, key: str) -> ProviderResult:
        """Return ABSENT only when lookup can authoritatively establish absence."""
        ...

    def cancel(self, key: str) -> ProviderResult:
        """CANCELLED confirms the call is stopped and a late start cannot reuse the key."""
        ...


class EventSource(Protocol):
    def poll_events(self, limit: int) -> list[ProviderEvent]: ...

    def ack_event(self, event_id: str) -> None: ...
