"""Tests for kombu.mixins."""

import asyncio

import pytest

from kombu import Connection, Queue
from kombu.exceptions import ConnectionError
from kombu.mixins import ConsumerMixin
from kombu.utils.json import dumps as json_dumps


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


class test_ConsumerMixin_run:
    """`run` starts over for a broker failure and for nothing else."""

    def _worker(self, get_consumers):
        conn = Connection("memory://")
        worker = Worker(conn)
        worker.create_connection = lambda: conn
        worker.get_consumers = get_consumers
        return worker

    @pytest.mark.usefixtures("_instant_sleep")
    async def test_a_broker_failure_starts_the_consumer_over(self):
        attempts = []

        def get_consumers(ConsumerFactory, channel):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("broker went away")
            worker.should_stop = True
            return []

        worker = self._worker(get_consumers)
        await worker.run()

        assert len(attempts) == 2

    @pytest.mark.usefixtures("_instant_sleep")
    async def test_a_programming_error_reaches_the_caller(self):
        def get_consumers(ConsumerFactory, channel):
            raise ValueError("typo in get_consumers")

        with pytest.raises(ValueError, match="typo in get_consumers"):
            await self._worker(get_consumers).run()


class test_ConsumerMixin_on_decode_error:
    async def test_the_mixin_handles_a_body_that_will_not_decode(self):
        seen = []

        class DecodeWorker(Worker):
            def get_consumers(self, ConsumerFactory, channel):
                return [ConsumerFactory(queues=[Queue("broken_q")], no_ack=True)]

            async def on_decode_error(self, message, exc):
                seen.append(exc)
                self.should_stop = True

        conn = Connection("memory://")
        await conn.connect()
        channel = await conn.default_channel()
        envelope = {
            "body": "{ this is not json",
            "content-type": "application/json",
            "content-encoding": "utf-8",
            "properties": {},
            "headers": {},
        }
        await channel.publish(
            message=json_dumps(envelope).encode("utf-8"),
            exchange="",
            routing_key="broken_q",
        )

        worker = DecodeWorker(conn)
        worker.create_connection = lambda: conn
        async for _ in worker.consume(limit=1):
            pass
        await conn.close()

        assert len(seen) == 1
