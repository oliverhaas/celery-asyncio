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
def _reset_transport_state():
    """Start every test with the process-wide transport state empty.

    Memory queues and declared exchanges outlive the connection that made
    them by design, so what a test leaves behind would reach the next one.
    """
    from kombu.transport.filesystem import Transport as FilesystemTransport
    from kombu.transport.memory import Transport as MemoryTransport

    yield
    MemoryTransport.reset_state()
    FilesystemTransport.reset_state()
