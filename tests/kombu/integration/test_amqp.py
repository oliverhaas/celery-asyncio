"""Integration tests for the AMQP transport against a live broker."""

import asyncio
import json
import os
import pickle
import uuid
from typing import Any

import pytest

from kombu import Connection, Exchange, Queue
from kombu.serialization import disable_insecure_serializers, enable_insecure_serializers

pytestmark = pytest.mark.asyncio(loop_scope="function")

AMQP_URL = os.environ.get("KOMBU_TEST_AMQP_URL", "amqp://guest:guest@localhost:5672//")


def envelope(
    payload: Any = None,
    properties: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
) -> bytes:
    """A kombu envelope, whose ``body`` is the *serialized* payload, not the payload itself."""
    return json.dumps(
        {
            "body": json.dumps({"v": 1} if payload is None else payload),
            "content-type": "application/json",
            "content-encoding": "utf-8",
            "properties": properties or {},
            "headers": headers or {},
        },
    ).encode()


@pytest.fixture
async def connection():
    conn = Connection(AMQP_URL)
    await conn.connect()
    yield conn
    await conn.close()


@pytest.fixture
async def channel(connection):
    return await connection.channel()


@pytest.fixture
async def queue(channel):
    """An auto-deleting queue with a name unique to the test that asked for it."""
    name = await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}", auto_delete=True))
    yield name
    await channel.queue_delete(name)


class TestPublishGet:
    async def test_round_trip(self, channel, queue):
        await channel.publish(envelope({"key": "hello"}), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg is not None
        assert msg.payload == {"key": "hello"}

    async def test_get_on_an_empty_queue_returns_none(self, channel, queue):
        assert await channel.get(queue, no_ack=True) is None

    async def test_headers_survive_the_round_trip(self, channel, queue):
        await channel.publish(envelope(headers={"task": "add", "id": "xyz"}), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg.headers == {"task": "add", "id": "xyz"}

    async def test_properties_survive_the_round_trip(self, channel, queue):
        properties = {
            "priority": 5,
            "delivery_mode": 2,
            "correlation_id": "corr-1",
            "reply_to": "reply-q",
            "message_id": "msg-1",
        }
        await channel.publish(envelope(properties=properties), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg.properties["priority"] == 5
        assert msg.properties["delivery_mode"] == 2
        assert msg.properties["correlation_id"] == "corr-1"
        assert msg.properties["reply_to"] == "reply-q"
        assert msg.properties["message_id"] == "msg-1"

    async def test_expiration_survives_the_round_trip(self, channel, queue):
        # The regression this file exists for: aio-pika decodes the expiration
        # header to a float of seconds, so reading it back used to raise
        # AttributeError on every message that carried a TTL.
        await channel.publish(envelope(properties={"expiration": "60000"}), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg.properties["expiration"] == "60000"

    async def test_delivery_info_names_the_queue(self, channel, queue):
        await channel.publish(envelope(), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg.delivery_info["routing_key"] == queue
        assert msg.delivery_info["exchange"] == ""
        assert msg.delivery_info["redelivered"] is False


class TestBinarySerializers:
    """Binary payloads have to reach the broker as bytes, byte for byte.

    The producer base64-wraps them to fit the JSON envelope and labels the
    content encoding "binary", which is not a Python codec: encoding the
    envelope body with it raised LookupError on every apply_async.
    """

    @pytest.fixture(autouse=True)
    def _allow_insecure_serializers(self):
        enable_insecure_serializers()
        yield
        disable_insecure_serializers()

    @pytest.mark.parametrize("serializer", ["pickle", "msgpack", "json"])
    async def test_round_trip(self, connection, channel, queue, serializer):
        payload = {"task": "add", "args": [1, 2], "blob": b"\xff\xfe\x00"}
        producer = connection.Producer(channel=channel)

        await producer.publish(payload, serializer=serializer, routing_key=queue)
        await asyncio.sleep(0.2)

        msg = await channel.get(queue, no_ack=True)
        assert msg is not None
        assert msg.payload == payload

    async def test_pickle_body_is_not_base64_on_the_wire(self, connection, channel, queue):
        payload = {"task": "add"}
        producer = connection.Producer(channel=channel)

        await producer.publish(payload, serializer="pickle", routing_key=queue)
        await asyncio.sleep(0.2)

        msg = await channel.get(queue, no_ack=True)
        assert msg.body == pickle.dumps(payload)
        assert msg.content_encoding == "binary"
        assert "body_encoding" not in msg.headers


class TestAcknowledgement:
    async def test_ack_removes_the_message(self, channel, queue):
        await channel.publish(envelope(), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=False)
        await msg.ack()

        assert await channel.get(queue, no_ack=True) is None

    async def test_reject_without_requeue_drops_the_message(self, channel, queue):
        await channel.publish(envelope(), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=False)
        await msg.reject(requeue=False)

        assert await channel.get(queue, no_ack=True) is None

    async def test_requeue_puts_the_message_back(self, channel, queue):
        await channel.publish(envelope({"v": "requeued"}), exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=False)
        await msg.requeue()

        redelivered = await channel.get(queue, no_ack=True)
        assert redelivered is not None
        assert redelivered.payload == {"v": "requeued"}
        assert redelivered.delivery_info["redelivered"] is True


class TestUnroutable:
    async def test_publish_to_a_nonexistent_queue_is_dropped(self, channel):
        # AMQP drops an unroutable message rather than reporting it back.
        await channel.publish(envelope(), exchange="", routing_key=f"kombu-it-missing-{uuid.uuid4().hex}")


class TestQueue:
    async def test_purge_reports_what_it_dropped(self, channel, queue):
        for _ in range(3):
            await channel.publish(envelope(), exchange="", routing_key=queue)
        # Publishes are confirmed, but the queue counter lags them slightly.
        await asyncio.sleep(0.2)

        assert await channel.queue_purge(queue) == 3
        assert await channel.get(queue, no_ack=True) is None

    async def test_server_names_an_anonymous_queue(self, channel):
        name = await channel.declare_queue(Queue("", auto_delete=True))
        assert name
        await channel.queue_delete(name)


class TestExchangeRouting:
    async def test_direct_exchange_routes_on_the_key(self, channel, queue):
        exchange = Exchange(f"kombu-it-direct-{uuid.uuid4().hex}", type="direct", auto_delete=True)
        await channel.declare_exchange(exchange)
        await channel.queue_bind(queue, exchange.name, routing_key="rk")

        await channel.publish(envelope({"v": "routed"}), exchange=exchange.name, routing_key="rk")
        await channel.publish(envelope({"v": "dropped"}), exchange=exchange.name, routing_key="other")
        await asyncio.sleep(0.2)

        msg = await channel.get(queue, no_ack=True)
        assert msg.payload == {"v": "routed"}
        assert await channel.get(queue, no_ack=True) is None

        await channel.exchange_delete(exchange.name)

    async def test_fanout_exchange_reaches_every_bound_queue(self, channel):
        exchange = Exchange(f"kombu-it-fanout-{uuid.uuid4().hex}", type="fanout", auto_delete=True)
        await channel.declare_exchange(exchange)
        names = [await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}", auto_delete=True)) for _ in range(2)]
        for name in names:
            await channel.queue_bind(name, exchange.name)

        await channel.publish(envelope({"v": "broadcast"}), exchange=exchange.name, routing_key="")
        await asyncio.sleep(0.2)

        for name in names:
            msg = await channel.get(name, no_ack=True)
            assert msg is not None, name
            assert msg.payload == {"v": "broadcast"}
            await channel.queue_delete(name)

        await channel.exchange_delete(exchange.name)


class TestConsume:
    async def test_consumer_receives_published_messages(self, channel, queue):
        received = []
        await channel.basic_consume(queue, callback=lambda body, message: received.append(body), no_ack=True)

        await channel.publish(envelope({"v": "consumed"}), exchange="", routing_key=queue)

        async with asyncio.timeout(5):
            while not received:
                await channel.drain_events(timeout=1)

        assert received[0] == {"v": "consumed"}

    async def test_cancel_stops_delivery(self, channel):
        # Deliberately not auto-delete: the broker drops an auto-delete queue as
        # soon as its last consumer cancels, leaving nothing to publish to.
        name = await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}"))
        received = []
        tag = await channel.basic_consume(
            name,
            callback=lambda body, message: received.append(body),
            no_ack=True,
        )
        await channel.basic_cancel(tag)

        await channel.publish(envelope(), exchange="", routing_key=name)
        await asyncio.sleep(0.3)

        assert received == []
        # The message is still on the queue, not silently eaten.
        assert await channel.get(name, no_ack=True) is not None

        await channel.queue_delete(name)
