from zlib import crc32

from smartdialer.providers.base import ProviderEventType
from smartdialer.providers.mock_fast import MockFastProvider, StartBehavior, Timeline

UNRELIABLE_PROVIDER = "mock_unreliable"


class MockUnreliableProvider(MockFastProvider):
    name = UNRELIABLE_PROVIDER
    timeline: Timeline = (
        (ProviderEventType.RINGING, 0.5, 2.8),
        (ProviderEventType.ANSWERED, 1.0, 1.0),
        (ProviderEventType.ANSWERED, 1.0, 1.2),
        (ProviderEventType.CONNECTED, 1.4, 1.4),
        (ProviderEventType.COMPLETED, 2.5, 2.5),
    )

    def _behavior(self, key: str) -> StartBehavior:
        if self._start_behavior is not None:
            return self._start_behavior
        return "timeout_after" if crc32(key.encode()) % 5 == 0 else "accept"
