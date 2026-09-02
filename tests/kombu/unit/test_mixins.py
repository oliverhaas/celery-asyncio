"""Tests for kombu.mixins."""

import asyncio

import pytest

from kombu import Connection
from kombu.mixins import ConsumerMixin


class Worker(ConsumerMixin):
    """A ConsumerMixin that records the connection errors it is told about."""

    def __init__(self, connection):
        self.connection = connection
        self.connection_errors = []

    def get_consumers(self, ConsumerFactory, channel):
        return []

    def on_connection_error(self, exc, interval):
        self.connection_errors.append((exc, interval))


@pytest.fixture
def _instant_sleep(monkeypatch):
    """Take the retry backoff out of the wall clock."""

    async def sleep(delay, result=None):
        return result

    monkeypatch.setattr(asyncio, "sleep", sleep)


class test_ConsumerMixin:
    @pytest.mark.usefixtures("_instant_sleep")
    async def test_connection_failure_reaches_on_connection_error(self):
        conn = Connection("memory://")
        worker = Worker(conn)
        worker.create_connection = lambda: conn
        failure = OSError("broker down")
        attempts = []
        connect = conn.connect

        async def flaky_connect():
            if not attempts:
                attempts.append(1)
                raise failure
            return await connect()

        conn.connect = flaky_connect

        async with worker.establish_connection() as established:
            assert established is conn

        assert worker.connection_errors == [(failure, 2.0)]

    @pytest.mark.usefixtures("_instant_sleep")
    async def test_connection_failure_keeps_retrying_until_it_connects(self):
        conn = Connection("memory://")
        worker = Worker(conn)
        worker.create_connection = lambda: conn
        attempts = []
        connect = conn.connect

        async def flaky_connect():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError(f"attempt {len(attempts)}")
            return await connect()

        conn.connect = flaky_connect

        async with worker.establish_connection():
            pass

        assert len(attempts) == 3
        assert [interval for _, interval in worker.connection_errors] == [2.0, 4.0]
