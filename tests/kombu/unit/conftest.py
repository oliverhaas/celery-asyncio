"""Pytest configuration for kombu-asyncio unit tests."""

import pytest

from tests.mocks import MockChannel, MockTransport


@pytest.fixture
def mock_transport():
    """Create a MockTransport instance."""
    return MockTransport()


@pytest.fixture
def mock_channel(mock_transport):
    """Create a MockChannel instance."""
    return MockChannel(transport=mock_transport)


@pytest.fixture(autouse=True)
def _reset_memory_transport():
    """Reset memory transport shared state between tests."""
    yield
    try:
        from kombu.transport.memory import Channel as MemoryChannel

        MemoryChannel._queues.clear()
        MemoryChannel._exchanges.clear()
        MemoryChannel._bindings.clear()
    except ImportError:
        pass
