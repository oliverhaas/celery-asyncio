"""Integration tests for kombu.messaging against live brokers.

Transport-agnostic: every test that can run on both runs on both.
"""

import os
import uuid

import pytest

from kombu import Connection, Exchange, Queue

pytestmark = pytest.mark.asyncio(loop_scope="function")

AMQP_URL = os.environ.get("KOMBU_TEST_AMQP_URL", "amqp://guest:guest@localhost:5672//")
REDIS_URL = os.environ.get("KOMBU_TEST_REDIS_URL", "redis://localhost:6379/15")

URLS = {"amqp": AMQP_URL, "redis": REDIS_URL}


def name(prefix):
    return f"kombu-it-{prefix}-{uuid.uuid4().hex}"


@pytest.fixture(params=sorted(URLS), ids=sorted(URLS))
async def connection(request):
    conn = Connection(URLS[request.param])
    await conn.connect()
    yield conn
    await conn.close()


@pytest.fixture
async def amqp_connection():
    conn = Connection(AMQP_URL)
    await conn.connect()
    yield conn
    await conn.close()


async def drain(conn, times=4, timeout=1.0):
    for _ in range(times):
        try:
            await conn.drain_events(timeout=timeout)
        except TimeoutError:
            return


class TestConsumeAfterAddQueue:
    async def test_a_queue_added_after_consume_receives_messages(self, connection):
        received = []
        exchange = Exchange(name("ex"), type="direct", auto_delete=True)
        first = Queue(name("first"), exchange=exchange, routing_key="first", auto_delete=True)
        second = Queue(name("second"), exchange=exchange, routing_key="second", auto_delete=True)

        consumer = connection.Consumer(
            [first],
            callbacks=[lambda body, message: received.append(body)],
            no_ack=True,
        )
        await consumer.consume()
        consumer.add_queue(second)
        await consumer.consume()

        try:
            async with connection.Producer(exchange=exchange, auto_declare=False) as producer:
                await producer.publish({"for": "second"}, routing_key="second")
            await drain(connection)
            assert received == [{"for": "second"}]
        finally:
            await consumer.cancel()
            channel = await connection.default_channel()
            await channel.queue_delete(first.name)
            await channel.queue_delete(second.name)


class TestCancelByQueue:
    async def test_the_broker_stops_delivering_from_the_cancelled_queue(self, connection):
        received = []
        first = Queue(name("first"))
        second = Queue(name("second"))

        consumer = connection.Consumer(
            [first, second],
            callbacks=[lambda body, message: received.append(body)],
            no_ack=True,
        )
        await consumer.consume()
        await consumer.cancel_by_queue(first.name)

        try:
            async with connection.Producer(auto_declare=False) as producer:
                await producer.publish({"for": "first"}, routing_key=first.name)
                await producer.publish({"for": "second"}, routing_key=second.name)
            await drain(connection)
            assert received == [{"for": "second"}]
        finally:
            await consumer.cancel()
            channel = await connection.default_channel()
            await channel.queue_delete(first.name)
            await channel.queue_delete(second.name)


class TestDeclareMismatch:
    @pytest.mark.amqp
    async def test_a_redeclare_with_different_arguments_reaches_the_caller(self, amqp_connection):
        queue_name = name("priority")
        channel = await amqp_connection.default_channel()
        await channel.declare_queue(Queue(queue_name, max_priority=10))

        other = Queue(queue_name, max_priority=5)
        try:
            producer = amqp_connection.Producer(auto_declare=False)
            with pytest.raises(amqp_connection.channel_errors):
                await producer.publish({"a": 1}, routing_key=queue_name, declare=[other])
        finally:
            cleanup = await amqp_connection.channel()
            await cleanup.queue_delete(queue_name)


class TestPublishRetry:
    @pytest.mark.amqp
    async def test_a_publish_survives_the_connection_dropping(self, amqp_connection):
        queue_name = name("retry")
        channel = await amqp_connection.default_channel()
        await channel.declare_queue(Queue(queue_name))
        reported = []

        try:
            # Hang up on the broker behind the producer's back, the way a
            # restarted or overloaded broker does.
            await amqp_connection.transport._connection.close()

            producer = amqp_connection.Producer(auto_declare=False)
            await producer.publish(
                {"survived": True},
                routing_key=queue_name,
                retry=True,
                retry_policy={
                    "max_retries": 3,
                    "interval_start": 0,
                    "interval_step": 0,
                    "errback": lambda exc, interval: reported.append(exc),
                },
            )

            assert len(reported) == 1
            # The reconnect left a new channel behind, which has to declare the
            # queue before it can get from it.
            channel = await amqp_connection.default_channel()
            await channel.declare_queue(Queue(queue_name))
            message = await channel.get(queue_name, no_ack=True)
            assert message.decode() == {"survived": True}
        finally:
            cleanup = await amqp_connection.channel()
            await cleanup.queue_delete(queue_name)
