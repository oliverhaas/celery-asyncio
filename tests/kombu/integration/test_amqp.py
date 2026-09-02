"""Integration tests for the AMQP transport against a live broker."""

import asyncio
import base64
import json
import os
import pickle
import shutil
import subprocess
import uuid
from typing import Any

import pytest

from kombu import Connection, Exchange, Queue
from kombu.compression import compress
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


class TestCompressedBodies:
    """A compressed body reaches the broker compressed and comes back decompressed.

    The producer compresses the serialized payload, base64-wraps the result to
    fit the JSON envelope and names the method in a ``compression`` header. The
    wrapping describes the envelope, so it stops at the transport; the header
    describes the message and travels with it, which is what lets the receiving
    Message decompress the body.
    """

    @pytest.mark.parametrize("method", ["zlib", "bzip2", "lzma", "zstd"])
    async def test_round_trip(self, channel, queue, method):
        payload = json.dumps({"task": "add", "args": list(range(200))}).encode()
        compressed, content_type = compress(payload, method)
        message = json.dumps(
            {
                "body": base64.b64encode(compressed).decode("ascii"),
                "content-type": "application/json",
                "content-encoding": "utf-8",
                "properties": {},
                "headers": {"body_encoding": "base64", "compression": content_type},
            },
        ).encode()

        await channel.publish(message, exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg is not None
        assert msg.body == payload
        assert msg.payload == {"task": "add", "args": list(range(200))}
        assert msg.headers == {"compression": content_type}

    async def test_the_compressed_bytes_are_what_travel(self, channel, queue):
        """The wire carries the compressed bytes, not the base64 of them.

        Published without the ``compression`` header, so that nothing
        decompresses the body on the way back and the wire bytes show.
        """
        payload = b"x" * 4096
        compressed, _ = compress(payload, "zlib")
        message = json.dumps(
            {
                "body": base64.b64encode(compressed).decode("ascii"),
                "content-type": "application/octet-stream",
                "content-encoding": "binary",
                "properties": {},
                "headers": {"body_encoding": "base64"},
            },
        ).encode()

        await channel.publish(message, exchange="", routing_key=queue)

        msg = await channel.get(queue, no_ack=True)
        assert msg.body == compressed
        assert len(compressed) < len(payload)


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


class TestPrefetch:
    """The prefetch window is the only backpressure AMQP gives a consumer.

    RabbitMQ answers channel.flow(active=false) with NOT_IMPLEMENTED, so
    without a prefetch the broker sends the whole queue at once and aiormq
    runs a task per delivery.
    """

    async def test_the_broker_sends_no_more_than_the_prefetch_allows(self):
        conn = Connection(AMQP_URL, transport_options={"prefetch_count": 2})
        await conn.connect()
        try:
            channel = await conn.channel()
            name = await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}", durable=True))
            for i in range(10):
                await channel.publish(envelope({"n": i}), exchange="", routing_key=name)

            await channel.basic_consume(name, callback=lambda body, message: None, no_ack=False)
            await asyncio.sleep(1.0)

            # Every delivery is tracked for acknowledgement until it is acked,
            # so this is what the broker has outstanding.
            assert len(channel._delivery_tag_map) == 2

            for tag in list(channel._delivery_tag_map):
                await channel.basic_ack(tag)
            await asyncio.sleep(1.0)

            assert len(channel._delivery_tag_map) == 2

            await channel.queue_delete(name)
        finally:
            await conn.close()

    async def test_a_prefetch_set_while_consuming_takes_effect(self):
        conn = Connection(AMQP_URL)
        await conn.connect()
        try:
            channel = await conn.channel()
            name = await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}", durable=True))
            await channel.basic_consume(name, callback=lambda body, message: None, no_ack=False)
            await channel.basic_qos(prefetch_count=2)

            for i in range(10):
                await channel.publish(envelope({"n": i}), exchange="", routing_key=name)
            await asyncio.sleep(1.0)

            assert len(channel._delivery_tag_map) == 2

            await channel.queue_delete(name)
        finally:
            await conn.close()


RABBITMQ_CONTAINER = os.environ.get("KOMBU_TEST_RABBITMQ_CONTAINER", "audit-rabbitmq")


def close_connection_server_side(name: str) -> None:
    """Make the broker drop the named connection, as a restart or a network cut would."""
    listing = subprocess.run(
        ["docker", "exec", RABBITMQ_CONTAINER, "rabbitmqctl", "list_connections", "pid", "client_properties"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in listing.splitlines():
        if name in line:
            pid = line.split("\t")[0]
            subprocess.run(
                ["docker", "exec", RABBITMQ_CONTAINER, "rabbitmqctl", "close_connection", pid, "test"],
                capture_output=True,
                check=True,
            )
            return
    raise AssertionError(f"connection {name} not found on the broker")


def rabbitmqctl_columns(*columns: str, match: str) -> str:
    """Return the last column of the connection listing row naming ``match``."""
    listing = subprocess.run(
        ["docker", "exec", RABBITMQ_CONTAINER, "rabbitmqctl", "list_connections", *columns],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in listing.splitlines():
        if match in line:
            return line.split("\t")[-1].strip()
    raise AssertionError(f"connection {match} not found on the broker")


@pytest.fixture
def rabbitmqctl_listing(_rabbitmqctl):
    return rabbitmqctl_columns


@pytest.fixture
def rabbitmqctl(_rabbitmqctl):
    return close_connection_server_side


@pytest.fixture
def _rabbitmqctl():
    if shutil.which("docker") is None:
        pytest.skip("docker is needed to close a connection from the broker side")
    probe = subprocess.run(
        ["docker", "exec", RABBITMQ_CONTAINER, "rabbitmqctl", "status"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"rabbitmqctl is not reachable in container {RABBITMQ_CONTAINER}")


class TestTransportOptions:
    async def test_the_heartbeat_reaches_the_broker(self, rabbitmqctl_listing):
        """aio-pika drops its keyword arguments once it is handed a URL.

        The heartbeat has to travel in the URL query, and the broker's timeout
        column is where it shows up.
        """
        name = f"kombu-it-{uuid.uuid4().hex}"
        conn = Connection(f"{AMQP_URL}?name={name}", transport_options={"heartbeat": 7})
        await conn.connect()
        try:
            assert rabbitmqctl_listing("client_properties", "timeout", match=name) == "7"
        finally:
            await conn.close()


class TestRecover:
    async def test_recover_redelivers_what_was_not_acknowledged(self, channel, queue):
        await channel.publish(envelope({"v": "unacked"}), exchange="", routing_key=queue)

        first = await channel.get(queue, no_ack=False)
        assert first is not None

        await channel.basic_recover(requeue=True)

        again = await channel.get(queue, no_ack=True)
        assert again is not None
        assert again.payload == {"v": "unacked"}


class TestConnectionLoss:
    """The broker drops the connection; the transport has to notice and recover."""

    @pytest.fixture
    async def named_connection(self):
        # aiormq reads the connection name from the URL query, and it is what
        # identifies this connection in rabbitmqctl's listing.
        name = f"kombu-it-{uuid.uuid4().hex}"
        conn = Connection(f"{AMQP_URL}?name={name}")
        await conn.connect()
        conn.test_name = name
        yield conn
        await conn.close()

    async def test_a_lost_connection_is_reported_and_then_reconnected(self, named_connection, rabbitmqctl):
        conn = named_connection
        channel = await conn.default_channel()
        name = await channel.declare_queue(Queue(f"kombu-it-{uuid.uuid4().hex}", durable=True))
        received = []
        await channel.basic_consume(name, callback=lambda body, message: received.append(body), no_ack=True)

        await channel.publish(envelope({"v": "before"}), exchange="", routing_key=name)
        async with asyncio.timeout(5):
            while not received:
                await channel.drain_events(timeout=1)

        rabbitmqctl(conn.test_name)
        async with asyncio.timeout(5):
            while conn.is_connected:
                await asyncio.sleep(0.05)

        with pytest.raises(conn.transport.connection_errors):
            await channel.drain_events(timeout=1)

        await conn.ensure_connection(max_retries=3)
        assert conn.is_connected

        # The same channel, its queue and its consumer come back on the new
        # connection, so a caller holding one keeps working.
        await channel.publish(envelope({"v": "after"}), exchange="", routing_key=name)
        async with asyncio.timeout(10):
            while len(received) < 2:
                await channel.drain_events(timeout=1)

        assert received == [{"v": "before"}, {"v": "after"}]
        await channel.queue_delete(name)

    async def test_ensure_connection_does_not_report_success_on_a_dead_connection(
        self,
        named_connection,
        rabbitmqctl,
    ):
        conn = named_connection
        await conn.default_channel()

        rabbitmqctl(conn.test_name)
        async with asyncio.timeout(5):
            while conn.is_connected:
                await asyncio.sleep(0.05)

        old_connection = conn.transport._connection
        await conn.ensure_connection(max_retries=3)

        assert conn.is_connected
        assert conn.transport._connection is not old_connection
