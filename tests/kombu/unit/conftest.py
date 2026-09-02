"""Pytest configuration for kombu unit tests."""

import pytest

from tests.kombu.mocks import MockChannel, MockTransport


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
    """Start every test with empty memory queues.

    The queues are process-wide by design, so messages and bindings a test
    leaves behind would otherwise reach the next one.
    """
    from kombu.transport.memory import Transport as MemoryTransport

    yield
    MemoryTransport.reset_state()
