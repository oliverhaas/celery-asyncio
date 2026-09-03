"""Unit tests for the AMQP transport (aio-pika wrapper).

All aio-pika objects are mocked, so no RabbitMQ broker is required.
"""

import asyncio
import base64
import json
import pickle
import ssl
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest

aio_pika = pytest.importorskip("aio_pika")

from aiormq import exceptions as aiormq_exc

from kombu.compression import compress
from kombu.entity import Exchange, Queue
from kombu.exceptions import ContentDisallowed
from kombu.message import Message
from kombu.transport.amqp import Channel, Transport, _get_exchange_type

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _CloseCallbacks:
    """Stand-in for aio-pika's CallbackCollection.

    MagicMock(spec=...) does not expose ``close_callbacks``, since aio-pika
    only annotates it on the abstract classes.
    """

    def __init__(self) -> None:
        self.callbacks = []

    def add(self, callback):
        self.callbacks.append(callback)

    def fire(self, sender, exc=None):
        for callback in list(self.callbacks):
            callback(sender, exc)


def _make_aio_channel(**overrides) -> MagicMock:
    """Create a mock aio-pika AbstractChannel."""
    ch = MagicMock(spec=aio_pika.abc.AbstractChannel)
    ch.is_closed = False
    ch.close = AsyncMock()
    ch.declare_exchange = AsyncMock()
    ch.declare_queue = AsyncMock()
    ch.exchange_delete = AsyncMock()
    ch.queue_delete = AsyncMock()
    ch.get_exchange = AsyncMock()
    ch.get_queue = AsyncMock()
    ch.set_qos = AsyncMock()
    ch.reopen = AsyncMock()
    ch.close_callbacks = _CloseCallbacks()

    # default_exchange
    default_ex = AsyncMock()
    default_ex.publish = AsyncMock()
    ch.default_exchange = default_ex

    for k, v in overrides.items():
        setattr(ch, k, v)
    return ch


def _make_aio_exchange(name: str = "test_ex") -> MagicMock:
    """Create a mock aio-pika AbstractExchange."""
    ex = MagicMock(spec=aio_pika.abc.AbstractExchange)
    ex.name = name
    ex.publish = AsyncMock()
    return ex


def _make_aio_queue(name: str = "test_q") -> MagicMock:
    """Create a mock aio-pika AbstractQueue."""
    q = MagicMock(spec=aio_pika.abc.AbstractQueue)
    q.name = name
    q.bind = AsyncMock()
    q.unbind = AsyncMock()
    q.purge = AsyncMock(return_value=SimpleNamespace(message_count=0))
    q.consume = AsyncMock(return_value=name)  # returns consumer tag
    q.cancel = AsyncMock()
    q.get = AsyncMock(return_value=None)
    return q


def _make_incoming_message(
    body: bytes = b'{"hello": "world"}',
    delivery_tag: int = 1,
    content_type: str = "application/json",
    content_encoding: str = "utf-8",
    exchange: str = "",
    routing_key: str = "test_q",
    headers: dict | None = None,
    priority: int | None = None,
    delivery_mode: aio_pika.DeliveryMode | None = None,
    expiration: float | timedelta | datetime | None = None,
    correlation_id: str | None = None,
    reply_to: str | None = None,
    message_id: str | None = None,
    timestamp: datetime | None = None,
    app_id: str | None = None,
    type: str | None = None,
    redelivered: bool = False,
) -> MagicMock:
    """Create a mock aio-pika IncomingMessage."""
    msg = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
    msg.body = body
    msg.delivery_tag = delivery_tag
    msg.content_type = content_type
    msg.content_encoding = content_encoding
    msg.exchange = exchange
    msg.routing_key = routing_key
    msg.headers = headers or {}
    msg.priority = priority
    msg.delivery_mode = delivery_mode
    msg.expiration = expiration
    msg.correlation_id = correlation_id
    msg.reply_to = reply_to
    msg.message_id = message_id
    msg.timestamp = timestamp
    msg.app_id = app_id
    msg.type = type
    msg.redelivered = redelivered
    msg.ack = AsyncMock()
    msg.reject = AsyncMock()
    return msg


def _make_aio_connection(**overrides) -> MagicMock:
    """Create a mock aio-pika AbstractConnection."""
    conn = MagicMock(spec=aio_pika.abc.AbstractConnection)
    conn.is_closed = False
    conn.close = AsyncMock()
    conn.channel = AsyncMock(side_effect=lambda **_: _make_aio_channel())
    conn.close_callbacks = _CloseCallbacks()
    conn.connected = asyncio.Event()
    conn.connected.set()
    for k, v in overrides.items():
        setattr(conn, k, v)
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_kombu_message(
    channel,
    body: bytes = b'{"x": 1}',
    delivery_tag: str = "1",
    consumer_tag: str = "",
) -> Message:
    return Message(
        body=body,
        delivery_tag=delivery_tag,
        content_type="application/json",
        content_encoding="utf-8",
        delivery_info={"consumer_tag": consumer_tag},
        channel=channel,
    )


@pytest.fixture
def aio_channel():
    return _make_aio_channel()


@pytest.fixture
def channel(aio_channel):
    return Channel(aio_channel)


# ---------------------------------------------------------------------------
# _get_exchange_type helper
# ---------------------------------------------------------------------------


class TestExchangeTypeHelper:
    def test_direct(self):
        assert _get_exchange_type("direct") == aio_pika.ExchangeType.DIRECT

    def test_fanout(self):
        assert _get_exchange_type("fanout") == aio_pika.ExchangeType.FANOUT

    def test_topic(self):
        assert _get_exchange_type("topic") == aio_pika.ExchangeType.TOPIC

    def test_headers(self):
        assert _get_exchange_type("headers") == aio_pika.ExchangeType.HEADERS

    def test_unknown_defaults_to_direct(self):
        assert _get_exchange_type("nonexistent") == aio_pika.ExchangeType.DIRECT


# ---------------------------------------------------------------------------
# Channel tests
# ---------------------------------------------------------------------------


class TestChannelInit:
    def test_initial_state(self, channel):
        assert channel._closed is False
        assert channel._consumers == {}
        assert channel.no_ack_consumers == set()
        assert channel._declared_exchanges == {}
        assert channel._declared_queues == {}
        assert channel._delivery_tag_map == {}
        assert channel._message_queue.empty()

    def test_wraps_aio_channel(self, channel, aio_channel):
        assert channel._aio_channel is aio_channel

    def test_message_queue_is_bounded_without_a_prefetch(self, channel):
        assert channel._message_queue.maxsize == 1000

    def test_message_queue_is_bounded_by_the_prefetch(self, aio_channel):
        assert Channel(aio_channel, prefetch_count=5)._message_queue.maxsize == 5


class TestChannelClose:
    async def test_close(self, channel, aio_channel):
        await channel.close()
        assert channel._closed is True
        aio_channel.close.assert_awaited_once()

    async def test_close_idempotent(self, channel, aio_channel):
        await channel.close()
        await channel.close()
        # Only one close call to underlying channel
        aio_channel.close.assert_awaited_once()

    async def test_close_cancels_consumers(self, aio_channel):
        ch = Channel(aio_channel)
        aio_queue = _make_aio_queue("q1")
        ch._declared_queues["q1"] = aio_queue
        ch._consumers["tag1"] = ("q1", lambda b, m: None, False)

        await ch.close()

        aio_queue.cancel.assert_awaited_once_with("tag1")
        assert ch._consumers == {}

    async def test_close_clears_delivery_tags(self, channel):
        channel._delivery_tag_map["1"] = MagicMock()
        channel._delivery_tag_map["2"] = MagicMock()

        await channel.close()

        assert channel._delivery_tag_map == {}

    async def test_close_handles_already_closed_aio_channel(self):
        aio_ch = _make_aio_channel()
        aio_ch.is_closed = True
        ch = Channel(aio_ch)

        await ch.close()

        aio_ch.close.assert_not_awaited()

    async def test_close_survives_a_channel_the_broker_took_away(self, aio_channel):
        aio_queue = _make_aio_queue("q1")
        aio_queue.cancel = AsyncMock(side_effect=aiormq_exc.ChannelInvalidStateError("gone"))
        ch = Channel(aio_channel)
        ch._declared_queues["q1"] = aio_queue
        ch._consumers["tag1"] = ("q1", lambda b, m: None, False)

        await ch.close()

        assert ch._closed is True
        assert ch._consumers == {}

    async def test_close_after_a_lost_connection_talks_to_no_broker(self, aio_channel):
        ch = Channel(aio_channel)
        ch._consumers["tag1"] = ("q1", lambda b, m: None, False)
        ch.on_connection_closed(ConnectionResetError("gone"))

        await ch.close()

        aio_channel.close.assert_not_awaited()
        assert ch._consumers == {}


class TestChannelExchange:
    async def test_declare_exchange(self, channel, aio_channel):
        ex = Exchange("my_exchange", type="direct", durable=True, auto_delete=False)
        mock_aio_ex = _make_aio_exchange("my_exchange")
        aio_channel.declare_exchange.return_value = mock_aio_ex

        await channel.declare_exchange(ex)

        aio_channel.declare_exchange.assert_awaited_once_with(
            name="my_exchange",
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
            auto_delete=False,
            arguments=None,
        )
        assert "my_exchange" in channel._declared_exchanges

    async def test_declare_exchange_with_arguments(self, channel, aio_channel):
        ex = Exchange("my_ex", type="headers", arguments={"x-match": "all"})
        aio_channel.declare_exchange.return_value = _make_aio_exchange("my_ex")

        await channel.declare_exchange(ex)

        _, kwargs = aio_channel.declare_exchange.call_args
        assert kwargs["arguments"] == {"x-match": "all"}

    async def test_declare_exchange_cached(self, channel, aio_channel):
        ex = Exchange("cached_ex", type="direct")
        aio_channel.declare_exchange.return_value = _make_aio_exchange("cached_ex")

        await channel.declare_exchange(ex)
        await channel.declare_exchange(ex)

        # Only one actual declare call
        assert aio_channel.declare_exchange.await_count == 1

    async def test_declare_exchange_skips_empty_name(self, channel, aio_channel):
        ex = Exchange("", type="direct")

        await channel.declare_exchange(ex)

        aio_channel.declare_exchange.assert_not_awaited()

    async def test_exchange_delete(self, channel, aio_channel):
        channel._declared_exchanges["doomed"] = _make_aio_exchange("doomed")

        await channel.exchange_delete("doomed")

        aio_channel.exchange_delete.assert_awaited_once_with("doomed")
        assert "doomed" not in channel._declared_exchanges

    async def test_exchange_delete_not_in_cache(self, channel, aio_channel):
        await channel.exchange_delete("uncached")
        aio_channel.exchange_delete.assert_awaited_once_with("uncached")


class TestChannelQueue:
    async def test_declare_queue(self, channel, aio_channel):
        q = Queue("my_queue", durable=True, exclusive=False, auto_delete=False)
        mock_aio_q = _make_aio_queue("my_queue")
        aio_channel.declare_queue.return_value = mock_aio_q

        result = await channel.declare_queue(q)

        assert result == "my_queue"
        aio_channel.declare_queue.assert_awaited_once_with(
            name="my_queue",
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments=None,
        )
        assert "my_queue" in channel._declared_queues

    async def test_declare_queue_server_generated_name(self, channel, aio_channel):
        q = Queue("")  # empty name => server-generated
        mock_aio_q = _make_aio_queue("amq.gen-abc123")
        aio_channel.declare_queue.return_value = mock_aio_q

        result = await channel.declare_queue(q)

        assert result == "amq.gen-abc123"
        assert q.name == "amq.gen-abc123"
        _, kwargs = aio_channel.declare_queue.call_args
        assert kwargs["name"] is None  # None triggers server-generated

    async def test_declare_queue_with_arguments(self, channel, aio_channel):
        q = Queue("ttl_q", queue_arguments={"x-message-ttl": 60000, "x-max-length": 100})
        aio_channel.declare_queue.return_value = _make_aio_queue("ttl_q")

        await channel.declare_queue(q)

        _, kwargs = aio_channel.declare_queue.call_args
        assert kwargs["arguments"] == {"x-message-ttl": 60000, "x-max-length": 100}

    async def test_declare_queue_no_queue_arguments(self, channel, aio_channel):
        q = Queue("plain_q")
        aio_channel.declare_queue.return_value = _make_aio_queue("plain_q")

        await channel.declare_queue(q)

        _, kwargs = aio_channel.declare_queue.call_args
        assert kwargs["arguments"] is None

    async def test_queue_bind(self, channel, aio_channel):
        aio_q = _make_aio_queue("q1")
        aio_ex = _make_aio_exchange("ex1")
        channel._declared_queues["q1"] = aio_q
        channel._declared_exchanges["ex1"] = aio_ex

        await channel.queue_bind("q1", "ex1", routing_key="rk1")

        aio_q.bind.assert_awaited_once_with(aio_ex, routing_key="rk1", arguments=None)

    async def test_queue_bind_fetches_undeclared_exchange(self, channel, aio_channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        fetched_ex = _make_aio_exchange("remote_ex")
        aio_channel.get_exchange.return_value = fetched_ex

        await channel.queue_bind("q1", "remote_ex", routing_key="rk")

        aio_channel.get_exchange.assert_awaited_once_with("remote_ex", ensure=False)
        assert channel._declared_exchanges["remote_ex"] is fetched_ex
        aio_q.bind.assert_awaited_once()

    async def test_queue_bind_default_exchange_skipped(self, channel):
        await channel.queue_bind("q1", "", routing_key="rk")

    async def test_queue_bind_asks_the_broker_about_an_undeclared_queue(self, channel, aio_channel):
        aio_q = _make_aio_queue("elsewhere")
        aio_channel.get_queue = AsyncMock(return_value=aio_q)
        aio_channel.get_exchange = AsyncMock(return_value=_make_aio_exchange("ex1"))

        await channel.queue_bind("elsewhere", "ex1", routing_key="rk")

        aio_channel.get_queue.assert_awaited_once_with("elsewhere", ensure=False)
        aio_q.bind.assert_awaited_once()

    async def test_queue_bind_with_arguments(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_ex = _make_aio_exchange("ex1")
        channel._declared_queues["q1"] = aio_q
        channel._declared_exchanges["ex1"] = aio_ex

        await channel.queue_bind("q1", "ex1", routing_key="rk", arguments={"x-match": "any"})

        aio_q.bind.assert_awaited_once_with(
            aio_ex,
            routing_key="rk",
            arguments={"x-match": "any"},
        )

    async def test_queue_unbind(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_ex = _make_aio_exchange("ex1")
        channel._declared_queues["q1"] = aio_q
        channel._declared_exchanges["ex1"] = aio_ex

        await channel.queue_unbind("q1", "ex1", routing_key="rk1")

        aio_q.unbind.assert_awaited_once_with(aio_ex, routing_key="rk1", arguments=None)

    async def test_queue_unbind_asks_the_broker_about_what_it_has_not_declared(self, channel, aio_channel):
        aio_q = _make_aio_queue("q1")
        aio_ex = _make_aio_exchange("ex1")
        aio_channel.get_queue = AsyncMock(return_value=aio_q)
        aio_channel.get_exchange = AsyncMock(return_value=aio_ex)

        await channel.queue_unbind("q1", "ex1", routing_key="rk")

        aio_q.unbind.assert_awaited_once_with(aio_ex, routing_key="rk", arguments=None)

    async def test_queue_purge(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_q.purge.return_value = SimpleNamespace(message_count=5)
        channel._declared_queues["q1"] = aio_q

        count = await channel.queue_purge("q1")

        assert count == 5
        aio_q.purge.assert_awaited_once()

    async def test_queue_purge_asks_the_broker_about_an_undeclared_queue(self, channel, aio_channel):
        aio_q = _make_aio_queue("elsewhere")
        aio_q.purge.return_value = SimpleNamespace(message_count=4)
        aio_channel.get_queue = AsyncMock(return_value=aio_q)

        assert await channel.queue_purge("elsewhere") == 4

        aio_channel.get_queue.assert_awaited_once_with("elsewhere", ensure=False)

    async def test_queue_purge_result_without_message_count(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_q.purge.return_value = object()  # no message_count attr
        channel._declared_queues["q1"] = aio_q

        count = await channel.queue_purge("q1")
        assert count == 0

    async def test_queue_delete(self, channel, aio_channel):
        channel._declared_queues["q1"] = _make_aio_queue("q1")
        aio_channel.queue_delete.return_value = SimpleNamespace(message_count=3)

        count = await channel.queue_delete("q1", if_unused=True, if_empty=True)

        assert count == 3
        aio_channel.queue_delete.assert_awaited_once_with("q1", if_unused=True, if_empty=True)
        assert "q1" not in channel._declared_queues

    async def test_queue_delete_defaults(self, channel, aio_channel):
        channel._declared_queues["q1"] = _make_aio_queue("q1")
        aio_channel.queue_delete.return_value = SimpleNamespace(message_count=0)

        await channel.queue_delete("q1")

        aio_channel.queue_delete.assert_awaited_once_with("q1", if_unused=False, if_empty=False)

    async def test_queue_delete_result_without_message_count(self, channel, aio_channel):
        channel._declared_queues["q1"] = _make_aio_queue("q1")
        aio_channel.queue_delete.return_value = object()

        count = await channel.queue_delete("q1")
        assert count == 0


class TestChannelPublish:
    async def test_publish_default_exchange(self, channel, aio_channel):
        envelope = (
            b'{"body": "hello", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="my_queue")

        aio_channel.default_exchange.publish.assert_awaited_once()
        call_args = aio_channel.default_exchange.publish.call_args
        aio_msg = call_args[0][0]
        assert isinstance(aio_msg, aio_pika.Message)
        assert aio_msg.body == b"hello"
        assert call_args[1]["routing_key"] == "my_queue"

    async def test_publish_named_exchange(self, channel, aio_channel):
        aio_ex = _make_aio_exchange("my_ex")
        channel._declared_exchanges["my_ex"] = aio_ex

        envelope = (
            b'{"body": "data", "content-type": "text/plain",'
            b' "content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="my_ex", routing_key="rk")

        aio_ex.publish.assert_awaited_once()
        aio_msg = aio_ex.publish.call_args[0][0]
        assert aio_msg.body == b"data"
        assert aio_msg.content_type == "text/plain"

    async def test_publish_fetches_unknown_exchange(self, channel, aio_channel):
        fetched_ex = _make_aio_exchange("remote_ex")
        aio_channel.get_exchange.return_value = fetched_ex

        envelope = b'{"body": "x", "content-type": "application/json", "content-encoding": "utf-8", "properties": {}, "headers": {}}'

        await channel.publish(envelope, exchange="remote_ex", routing_key="rk")

        aio_channel.get_exchange.assert_awaited_once_with("remote_ex", ensure=False)
        fetched_ex.publish.assert_awaited_once()

    async def test_publish_binary_body_travels_as_bytes(self, channel, aio_channel):
        raw = pickle.dumps({"task": "add", "args": (1, 2)})
        envelope = json.dumps(
            {
                "body": base64.b64encode(raw).decode("ascii"),
                "content-type": "application/x-python-serialize",
                "content-encoding": "binary",
                "properties": {},
                "headers": {"body_encoding": "base64", "task": "add"},
            },
        ).encode()

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.body == raw
        assert aio_msg.content_encoding == "binary"
        assert aio_msg.headers == {"task": "add"}

    async def test_publish_base64_body_without_binary_encoding(self, channel, aio_channel):
        raw = b"\xff\xfe\x00 not text"
        envelope = json.dumps(
            {
                "body": base64.b64encode(raw).decode("ascii"),
                "content-type": "application/data",
                "content-encoding": "utf-8",
                "properties": {},
                "headers": {"body_encoding": "base64"},
            },
        ).encode()

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.body == raw

    async def test_publish_keeps_the_compression_header_and_drops_the_wrapper(self, channel, aio_channel):
        """body_encoding describes the envelope, compression describes the message."""
        compressed, content_type = compress(b"payload", "zlib")
        envelope = json.dumps(
            {
                "body": base64.b64encode(compressed).decode("ascii"),
                "content-type": "application/json",
                "content-encoding": "utf-8",
                "properties": {},
                "headers": {"body_encoding": "base64", "compression": content_type},
            },
        ).encode()

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.body == compressed
        assert aio_msg.headers == {"compression": content_type}

    async def test_publish_with_priority(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {"priority": 5}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.priority == 5

    async def test_publish_with_delivery_mode(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {"delivery_mode": 2}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

    async def test_publish_with_expiration(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {"expiration": "60000"}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.expiration == timedelta(milliseconds=60000)

    async def test_publish_with_correlation_id_and_reply_to(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8",'
            b' "properties": {"correlation_id": "task-123", "reply_to": "results"},'
            b' "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.correlation_id == "task-123"
        assert aio_msg.reply_to == "results"

    async def test_publish_preserves_headers(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {},'
            b' "headers": {"task": "myapp.add", "id": "abc-123"}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.headers == {"task": "myapp.add", "id": "abc-123"}

    async def test_publish_with_message_id(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8",'
            b' "properties": {"message_id": "msg-789"},'
            b' "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.message_id == "msg-789"

    async def test_publish_empty_headers(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        # Empty dict → headers={} or None (aio-pika normalises)
        assert not aio_msg.headers

    async def test_publish_no_properties(self, channel, aio_channel):
        """Envelope with empty properties: body and content type are correct,
        no explicit AMQP property fields were set from the envelope."""
        envelope = (
            b'{"body": "plain", "content-type": "text/plain",'
            b' "content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.body == b"plain"
        assert aio_msg.content_type == "text/plain"
        assert aio_msg.correlation_id is None
        assert aio_msg.reply_to is None

    async def test_publish_with_timestamp(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8",'
            b' "properties": {"timestamp": 1700000000.0},'
            b' "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        expected_ts = datetime.fromtimestamp(1700000000.0, tz=UTC)
        assert aio_msg.timestamp == expected_ts

    async def test_publish_with_app_id_and_type(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8",'
            b' "properties": {"app_id": "celery", "type": "myapp.add"},'
            b' "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.app_id == "celery"
        assert aio_msg.type == "myapp.add"

    async def test_publish_transient_delivery_mode(self, channel, aio_channel):
        envelope = (
            b'{"body": "x", "content-type": "application/json",'
            b' "content-encoding": "utf-8",'
            b' "properties": {"delivery_mode": 1}, "headers": {}}'
        )

        await channel.publish(envelope, exchange="", routing_key="q")

        aio_msg = aio_channel.default_exchange.publish.call_args[0][0]
        assert aio_msg.delivery_mode == aio_pika.DeliveryMode.NOT_PERSISTENT


class TestChannelGet:
    async def test_get_message(self, channel):
        incoming = _make_incoming_message(body=b'{"key": "val"}', delivery_tag=42)
        aio_q = _make_aio_queue("q1")
        aio_q.get.return_value = incoming
        channel._declared_queues["q1"] = aio_q

        msg = await channel.get("q1", no_ack=False)

        assert msg is not None
        assert isinstance(msg, Message)
        assert msg.body == b'{"key": "val"}'
        assert msg.delivery_tag == "42"
        assert msg.content_type == "application/json"
        # Should be tracked for ack
        assert "42" in channel._delivery_tag_map

    async def test_get_no_ack(self, channel):
        incoming = _make_incoming_message(delivery_tag=7)
        aio_q = _make_aio_queue("q1")
        aio_q.get.return_value = incoming
        channel._declared_queues["q1"] = aio_q

        msg = await channel.get("q1", no_ack=True)

        assert msg is not None
        # Should NOT be tracked
        assert "7" not in channel._delivery_tag_map

    async def test_get_empty_queue(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_q.get.return_value = None
        channel._declared_queues["q1"] = aio_q

        msg = await channel.get("q1")

        assert msg is None

    async def test_get_asks_the_broker_about_a_queue_it_has_not_declared(self, channel, aio_channel):
        """A queue this channel did not declare may still exist on the broker.

        Answering with None would make an existing queue look empty and a
        missing one look the same, instead of the broker's 404.
        """
        aio_q = _make_aio_queue("elsewhere")
        aio_q.get = AsyncMock(return_value=None)
        aio_channel.get_queue = AsyncMock(return_value=aio_q)

        assert await channel.get("elsewhere") is None

        aio_channel.get_queue.assert_awaited_once_with("elsewhere", ensure=False)
        aio_q.get.assert_awaited_once_with(no_ack=False, fail=False)

    async def test_get_forwards_accept_to_the_message(self, channel):
        incoming = _make_incoming_message(
            body=b"\x80\x05\x95",
            content_type="application/x-python-serialize",
            content_encoding="binary",
        )
        aio_q = _make_aio_queue("q1")
        aio_q.get.return_value = incoming
        channel._declared_queues["q1"] = aio_q

        msg = await channel.get("q1", no_ack=True, accept={"application/json"})

        assert msg.accept == {"application/json"}
        with pytest.raises(ContentDisallowed):
            msg.decode()


class TestChannelConsume:
    async def test_basic_consume(self, channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        callback = MagicMock()

        tag = await channel.basic_consume("q1", callback, consumer_tag="my-tag", no_ack=False)

        assert tag == "my-tag"
        assert "my-tag" in channel._consumers
        q_name, cb, no_ack = channel._consumers["my-tag"]
        assert q_name == "q1"
        assert cb is callback
        assert no_ack is False
        aio_q.consume.assert_awaited_once()

    async def test_basic_consume_no_ack(self, channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="noack-tag", no_ack=True)

        assert "noack-tag" in channel.no_ack_consumers

    async def test_basic_consume_auto_tag(self, channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        tag = await channel.basic_consume("q1", MagicMock())

        assert tag  # auto-generated UUID
        assert tag in channel._consumers

    async def test_basic_consume_registers_aio_consumer(self, channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")

        # aio-pika consume was called with our tag
        _, kwargs = aio_q.consume.call_args
        assert kwargs["consumer_tag"] == "tag1"

    async def test_basic_cancel(self, channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        channel._consumers["tag1"] = ("q1", MagicMock(), False)

        await channel.basic_cancel("tag1")

        assert "tag1" not in channel._consumers
        aio_q.cancel.assert_awaited_once_with("tag1")

    async def test_basic_cancel_removes_no_ack(self, channel):
        channel._consumers["tag1"] = ("q1", MagicMock(), True)
        channel.no_ack_consumers.add("tag1")

        await channel.basic_cancel("tag1")

        assert "tag1" not in channel.no_ack_consumers

    async def test_basic_cancel_unknown_tag(self, channel):
        # Should not raise
        await channel.basic_cancel("nonexistent")

    async def test_basic_consume_looks_up_a_queue_declared_elsewhere(self, channel, aio_channel):
        aio_q = _make_aio_queue("elsewhere")
        aio_channel.get_queue = AsyncMock(return_value=aio_q)

        tag = await channel.basic_consume("elsewhere", MagicMock(), consumer_tag="tag1")

        assert tag == "tag1"
        aio_channel.get_queue.assert_awaited_once_with("elsewhere", ensure=False)
        assert aio_q.consume.await_args.kwargs["consumer_tag"] == "tag1"

    async def test_basic_cancel_reports_a_failed_cancel(self, channel):
        aio_q = _make_aio_queue("q1")
        aio_q.cancel = AsyncMock(side_effect=aiormq_exc.ChannelNotFoundEntity("no such consumer"))
        channel._declared_queues["q1"] = aio_q
        channel._consumers["tag1"] = ("q1", MagicMock(), False)

        with pytest.raises(aiormq_exc.ChannelNotFoundEntity):
            await channel.basic_cancel("tag1")


class TestChannelDrainEvents:
    async def test_drain_events_delivers_message(self, channel):
        msg = Message(
            body=b'{"x": 1}',
            delivery_tag="1",
            content_type="application/json",
            content_encoding="utf-8",
            channel=channel,
        )
        received = []

        def callback(body, message):
            received.append((body, message))

        channel._consumers["tag1"] = ("q1", callback, False)
        await channel._message_queue.put(("q1", msg))

        result = await channel.drain_events(timeout=1.0)

        assert result is True
        assert len(received) == 1
        assert received[0][1] is msg

    async def test_drain_events_timeout(self, channel):
        channel._consumers["tag1"] = ("q1", MagicMock(), False)

        result = await channel.drain_events(timeout=0.01)

        assert result is False

    async def test_drain_events_leaves_no_task_pending_after_a_timeout(self, channel):
        """A getter only cancelled is destroyed pending if the loop closes next."""
        before = asyncio.all_tasks()

        await channel.drain_events(timeout=0.01)

        assert asyncio.all_tasks() == before

    async def test_drain_events_no_consumers(self, channel):
        result = await channel.drain_events(timeout=0.01)
        assert result is False

    async def test_drain_events_with_a_zero_timeout_delivers_a_buffered_message(self, channel):
        """A zero timeout is a poll, not an instant giving up.

        The worker drains its backlog with timeout=0 in a tight loop, so a
        poll that reports "nothing there" while the buffer is full costs a
        pass through the outer loop per message.
        """
        received = []
        channel._consumers["tag1"] = ("q1", lambda body, message: received.append(body), False)
        await channel._message_queue.put(("q1", _make_kombu_message(channel, b'{"x": 1}')))

        assert await channel.drain_events(timeout=0) is True
        assert received == [{"x": 1}]
        assert await channel.drain_events(timeout=0) is False

    async def test_drain_events_raises_when_the_connection_goes_while_waiting(self, channel):
        channel._consumers["tag1"] = ("q1", MagicMock(), False)
        drain = asyncio.ensure_future(channel.drain_events(timeout=5))
        await asyncio.sleep(0)

        channel.on_connection_closed(ConnectionResetError("broker went away"))

        with pytest.raises(Transport.connection_errors):
            await drain

    async def test_drain_events_raises_when_the_connection_is_already_gone(self, channel):
        channel.on_connection_closed(ConnectionResetError("broker went away"))

        with pytest.raises(Transport.connection_errors):
            await channel.drain_events(timeout=0)

    async def test_drain_events_async_callback(self, channel):
        msg = Message(
            body=b'{"y": 2}',
            delivery_tag="2",
            content_type="application/json",
            content_encoding="utf-8",
            channel=channel,
        )
        received = []

        async def async_callback(body, message):
            received.append(body)

        channel._consumers["tag1"] = ("q1", async_callback, False)
        await channel._message_queue.put(("q1", msg))

        await channel.drain_events(timeout=1.0)

        assert len(received) == 1

    async def test_on_incoming_buffers_message(self, channel):
        """Test the internal callback that aio-pika invokes."""
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1", no_ack=False)

        # Extract the callback that was passed to aio_queue.consume
        consume_call = aio_q.consume.call_args
        internal_callback = consume_call[1]["callback"]

        # Simulate aio-pika delivering a message
        incoming = _make_incoming_message(delivery_tag=99, body=b'{"buffered": true}')
        await internal_callback(incoming)

        # Message should be in the buffer
        assert not channel._message_queue.empty()
        queue_name, kombu_msg = channel._message_queue.get_nowait()
        assert queue_name == "q1"
        assert kombu_msg.delivery_tag == "99"
        # delivery_tag_map should track it for ack
        assert "99" in channel._delivery_tag_map

    async def test_a_delivery_that_cannot_be_accepted_goes_back_to_the_broker(self, channel, caplog):
        """aiormq never looks at the delivery task, so nothing here may be lost."""
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")
        internal_callback = aio_q.consume.call_args[1]["callback"]

        incoming = _make_incoming_message(delivery_tag=99)
        incoming.nack = AsyncMock()
        with patch.object(channel, "_convert_message", side_effect=ValueError("unreadable properties")):
            with caplog.at_level("ERROR", logger="kombu.transport.amqp"):
                await internal_callback(incoming)

        incoming.nack.assert_awaited_once_with(requeue=True)
        assert channel._message_queue.empty()
        assert "99" not in channel._delivery_tag_map
        assert "Failed to accept delivery 99 on queue q1" in caplog.text

    async def test_a_delivery_a_no_ack_consumer_cannot_accept_is_only_reported(self, channel, caplog):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1", no_ack=True)
        internal_callback = aio_q.consume.call_args[1]["callback"]

        incoming = _make_incoming_message(delivery_tag=99)
        incoming.nack = AsyncMock()
        with patch.object(channel, "_convert_message", side_effect=ValueError("unreadable properties")):
            with caplog.at_level("ERROR", logger="kombu.transport.amqp"):
                await internal_callback(incoming)

        incoming.nack.assert_not_awaited()
        assert "Failed to accept delivery 99 on queue q1" in caplog.text

    async def test_on_incoming_includes_consumer_tag(self, channel):
        """Internal callback passes consumer_tag to _convert_message."""
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="my-ctag", no_ack=False)

        internal_callback = aio_q.consume.call_args[1]["callback"]
        incoming = _make_incoming_message(delivery_tag=77)
        await internal_callback(incoming)

        _queue_name, kombu_msg = channel._message_queue.get_nowait()
        assert kombu_msg.delivery_info["consumer_tag"] == "my-ctag"

    async def test_on_incoming_includes_redelivered(self, channel):
        """Internal callback preserves redelivered flag."""
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1", no_ack=True)

        internal_callback = aio_q.consume.call_args[1]["callback"]
        incoming = _make_incoming_message(delivery_tag=88, redelivered=True)
        await internal_callback(incoming)

        _queue_name, kombu_msg = channel._message_queue.get_nowait()
        assert kombu_msg.delivery_info["redelivered"] is True

    async def test_on_incoming_no_ack_not_tracked(self, channel):
        """no_ack messages should not be added to _delivery_tag_map."""
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q

        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1", no_ack=True)

        internal_callback = aio_q.consume.call_args[1]["callback"]
        incoming = _make_incoming_message(delivery_tag=55)
        await internal_callback(incoming)

        assert "55" not in channel._delivery_tag_map

    async def test_drain_events_no_matching_consumer(self, channel):
        """Message for a queue with no registered consumer is silently dropped."""
        msg = Message(
            body=b'"orphan"',
            delivery_tag="99",
            content_type="application/json",
            content_encoding="utf-8",
            channel=channel,
        )
        channel._consumers["tag1"] = ("q1", MagicMock(), False)
        incoming = _make_incoming_message(delivery_tag=99)
        channel._delivery_tag_map["99"] = incoming
        await channel._message_queue.put(("other_queue", msg))

        assert await channel.drain_events(timeout=1.0) is True
        incoming.reject.assert_awaited_once_with(requeue=True)

    async def test_drain_events_reports_a_body_it_cannot_decode(self, channel, caplog):
        """The callback still gets the message, but the reason is not lost."""
        msg = MagicMock(spec=Message)
        msg.body = b"raw bytes"
        msg.delivery_tag = "7"
        msg.decode.side_effect = ContentDisallowed("refusing to deserialize")

        received = []
        channel._consumers["tag1"] = ("q1", lambda body, message: received.append(body), False)
        await channel._message_queue.put(("q1", msg))

        with caplog.at_level("ERROR", logger="kombu.transport.amqp"):
            await channel.drain_events(timeout=1.0)

        assert received == [b"raw bytes"]
        assert "Failed to decode message 7 from queue q1" in caplog.text
        assert "ContentDisallowed" in caplog.text

    async def test_drain_events_multiple_sequential(self, channel):
        """Multiple sequential drain_events calls deliver messages in order."""
        bodies = []

        def callback(body, message):
            bodies.append(body)

        channel._consumers["tag1"] = ("q1", callback, False)

        for i in range(3):
            msg = Message(
                body=f'{{"n": {i}}}'.encode(),
                delivery_tag=str(i),
                content_type="application/json",
                content_encoding="utf-8",
                channel=channel,
            )
            await channel._message_queue.put(("q1", msg))

        for _ in range(3):
            assert await channel.drain_events(timeout=1.0) is True

        assert len(bodies) == 3


class TestChannelAckReject:
    async def test_basic_ack(self, channel):
        incoming = _make_incoming_message(delivery_tag=10)
        channel._delivery_tag_map["10"] = incoming

        await channel.basic_ack("10")

        incoming.ack.assert_awaited_once_with(multiple=False)
        assert "10" not in channel._delivery_tag_map

    async def test_basic_ack_multiple(self, channel):
        incoming = _make_incoming_message(delivery_tag=10)
        channel._delivery_tag_map["10"] = incoming

        await channel.basic_ack("10", multiple=True)

        incoming.ack.assert_awaited_once_with(multiple=True)

    async def test_basic_ack_multiple_forgets_the_tags_it_covers(self, channel):
        for tag in (8, 9, 10, 11):
            channel._delivery_tag_map[str(tag)] = _make_incoming_message(delivery_tag=tag)

        await channel.basic_ack("10", multiple=True)

        assert sorted(channel._delivery_tag_map) == ["11"]

    async def test_ack_of_a_tag_below_a_multiple_ack_is_not_resent(self, channel):
        lower = _make_incoming_message(delivery_tag=8)
        channel._delivery_tag_map["8"] = lower
        channel._delivery_tag_map["10"] = _make_incoming_message(delivery_tag=10)

        await channel.basic_ack("10", multiple=True)
        await channel.basic_ack("8")

        lower.ack.assert_not_awaited()

    async def test_basic_ack_unknown_tag(self, channel):
        # Should not raise
        await channel.basic_ack("nonexistent")

    async def test_basic_reject(self, channel):
        incoming = _make_incoming_message(delivery_tag=20)
        channel._delivery_tag_map["20"] = incoming

        await channel.basic_reject("20", requeue=True)

        incoming.reject.assert_awaited_once_with(requeue=True)
        assert "20" not in channel._delivery_tag_map

    async def test_basic_reject_no_requeue(self, channel):
        incoming = _make_incoming_message(delivery_tag=20)
        channel._delivery_tag_map["20"] = incoming

        await channel.basic_reject("20", requeue=False)

        incoming.reject.assert_awaited_once_with(requeue=False)

    async def test_basic_reject_unknown_tag(self, channel):
        await channel.basic_reject("nonexistent")

    @pytest.mark.parametrize("requeue", [True, False])
    async def test_basic_recover(self, channel, aio_channel, requeue):
        underlay = MagicMock()
        underlay.basic_recover = AsyncMock()
        aio_channel.get_underlay_channel = AsyncMock(return_value=underlay)

        await channel.basic_recover(requeue=requeue)

        underlay.basic_recover.assert_awaited_once_with(requeue=requeue)

    async def test_basic_recover_reports_a_channel_that_is_not_open(self, channel, aio_channel):
        aio_channel.get_underlay_channel = AsyncMock(
            side_effect=aiormq_exc.ChannelInvalidStateError("Channel was not opened"),
        )

        with pytest.raises(Transport.channel_errors):
            await channel.basic_recover()

    async def test_basic_qos(self, channel, aio_channel):
        await channel.basic_qos(prefetch_count=10)

        aio_channel.set_qos.assert_awaited_once_with(prefetch_count=10)

    async def test_basic_qos_registers_running_consumers_again(self, channel, aio_channel):
        """RabbitMQ fixes a consumer's credit when it is registered.

        A prefetch set afterwards reaches nothing that is already consuming,
        so the consumers have to be registered again for it to take effect.
        """
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")
        aio_q.consume.reset_mock()

        await channel.basic_qos(prefetch_count=3)

        aio_q.cancel.assert_awaited_once_with("tag1")
        assert aio_q.consume.await_args.kwargs["consumer_tag"] == "tag1"
        assert channel._prefetch_count == 3

    async def test_basic_qos_leaves_a_no_ack_consumer_registered(self, channel, aio_channel):
        """Cancelling one would delete the auto-delete queue under it."""
        aio_q = _make_aio_queue("mailbox")
        channel._declared_queues["mailbox"] = aio_q
        await channel.basic_consume("mailbox", MagicMock(), consumer_tag="tag1", no_ack=True)
        aio_q.consume.reset_mock()

        await channel.basic_qos(prefetch_count=3)

        aio_q.cancel.assert_not_awaited()
        aio_q.consume.assert_not_awaited()
        assert channel._prefetch_count == 3

    async def test_basic_qos_leaves_consumers_alone_when_nothing_changes(self, channel, aio_channel):
        aio_q = _make_aio_queue("q1")
        channel._declared_queues["q1"] = aio_q
        channel._prefetch_count = 3
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")
        aio_q.consume.reset_mock()

        await channel.basic_qos(prefetch_count=3)

        aio_q.cancel.assert_not_awaited()
        aio_q.consume.assert_not_awaited()

    async def test_basic_qos_unlimited(self, channel, aio_channel):
        await channel.basic_qos(prefetch_count=0)

        aio_channel.set_qos.assert_awaited_once_with(prefetch_count=0)


class TestChannelConvertMessage:
    def test_base64_body_is_decoded(self, channel):
        raw = b"\x80\x05\x95 binary"
        incoming = _make_incoming_message(
            body=base64.b64encode(raw),
            content_type="application/x-python-serialize",
            content_encoding="binary",
            headers={"body_encoding": "base64", "task": "add"},
        )

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.body == raw
        assert msg.headers == {"task": "add"}

    def test_body_is_left_alone_without_a_body_encoding_header(self, channel):
        raw = b"\x80\x05\x95 binary"
        incoming = _make_incoming_message(body=raw, content_encoding="binary")

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.body == raw

    def test_basic_conversion(self, channel):
        incoming = _make_incoming_message(
            body=b"test body",
            delivery_tag=42,
            content_type="text/plain",
            content_encoding="ascii",
            exchange="my_ex",
            routing_key="my_rk",
        )

        msg = channel._convert_message(incoming, "q1", "42")

        assert isinstance(msg, Message)
        assert msg.body == b"test body"
        assert msg.delivery_tag == "42"
        assert msg.content_type == "text/plain"
        assert msg.content_encoding == "ascii"
        assert msg.delivery_info["exchange"] == "my_ex"
        assert msg.delivery_info["routing_key"] == "my_rk"
        assert msg.delivery_info["delivery_tag"] == "42"
        assert msg.delivery_info["consumer_tag"] == ""
        assert msg.delivery_info["redelivered"] is False
        assert msg.channel is channel

    def test_all_properties_mapped(self, channel):
        incoming = _make_incoming_message(
            delivery_tag=1,
            priority=5,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            expiration=30.0,
            correlation_id="corr-123",
            reply_to="reply-queue",
            message_id="msg-456",
        )

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.properties["priority"] == 5
        assert msg.properties["delivery_mode"] == 2
        assert msg.properties["expiration"] == "30000"
        assert msg.properties["correlation_id"] == "corr-123"
        assert msg.properties["reply_to"] == "reply-queue"
        assert msg.properties["message_id"] == "msg-456"
        assert msg.properties["delivery_tag"] == "1"

    def test_expiration_accepts_every_aio_pika_form(self, channel):
        # aio-pika hands us a float of seconds on the incoming path, but the
        # same attribute is a timedelta or an absolute datetime on messages we
        # built ourselves.
        for expiration in (30.0, timedelta(seconds=30)):
            incoming = _make_incoming_message(expiration=expiration)
            got = channel._convert_message(incoming, "q1", "1").properties["expiration"]
            assert got == "30000", f"{expiration!r} -> {got}"

        incoming = _make_incoming_message(expiration=datetime.now(UTC) + timedelta(seconds=30))
        # Relative to now, so the exact value depends on how long the call took.
        assert 29000 < int(channel._convert_message(incoming, "q1", "1").properties["expiration"]) <= 30000

    def test_none_properties_omitted(self, channel):
        incoming = _make_incoming_message(
            priority=None,
            delivery_mode=None,
            expiration=None,
            correlation_id=None,
            reply_to=None,
            message_id=None,
        )

        msg = channel._convert_message(incoming, "q1", "1")

        assert "priority" not in msg.properties
        assert "delivery_mode" not in msg.properties
        assert "expiration" not in msg.properties
        assert "correlation_id" not in msg.properties
        assert "reply_to" not in msg.properties
        assert "message_id" not in msg.properties

    def test_headers_preserved(self, channel):
        incoming = _make_incoming_message(headers={"task": "add", "id": "xyz"})

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.headers == {"task": "add", "id": "xyz"}

    def test_empty_headers(self, channel):
        incoming = _make_incoming_message(headers={})

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.headers == {}

    def test_null_content_type_defaults(self, channel):
        incoming = _make_incoming_message(content_type=None, content_encoding=None)

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.content_type == "application/octet-stream"
        assert msg.content_encoding == "utf-8"

    def test_null_exchange_and_routing_key(self, channel):
        incoming = _make_incoming_message(exchange=None, routing_key=None)

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.delivery_info["exchange"] == ""
        assert msg.delivery_info["routing_key"] == ""

    def test_timestamp_app_id_type_mapped(self, channel):
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        incoming = _make_incoming_message(
            delivery_tag=1,
            timestamp=ts,
            app_id="celery",
            type="myapp.add",
        )

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.properties["timestamp"] == ts.timestamp()
        assert msg.properties["app_id"] == "celery"
        assert msg.properties["type"] == "myapp.add"

    def test_none_timestamp_app_id_type_omitted(self, channel):
        incoming = _make_incoming_message(
            delivery_tag=1,
            timestamp=None,
            app_id=None,
            type=None,
        )

        msg = channel._convert_message(incoming, "q1", "1")

        assert "timestamp" not in msg.properties
        assert "app_id" not in msg.properties
        assert "type" not in msg.properties

    def test_none_headers_from_incoming(self, channel):
        incoming = _make_incoming_message()
        incoming.headers = None

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.headers == {}

    def test_consumer_tag_in_delivery_info(self, channel):
        incoming = _make_incoming_message()

        msg = channel._convert_message(incoming, "q1", "1", consumer_tag="my-consumer")

        assert msg.delivery_info["consumer_tag"] == "my-consumer"

    def test_redelivered_true(self, channel):
        incoming = _make_incoming_message(redelivered=True)

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.delivery_info["redelivered"] is True

    def test_redelivered_false_by_default(self, channel):
        incoming = _make_incoming_message()

        msg = channel._convert_message(incoming, "q1", "1")

        assert msg.delivery_info["redelivered"] is False

    def test_delivery_tag_in_delivery_info(self, channel):
        incoming = _make_incoming_message(delivery_tag=42)

        msg = channel._convert_message(incoming, "q1", "42")

        assert msg.delivery_info["delivery_tag"] == "42"


class TestChannelContextManager:
    async def test_aenter_returns_self(self, channel):
        result = await channel.__aenter__()
        assert result is channel

    async def test_aexit_closes(self, channel, aio_channel):
        await channel.__aexit__(None, None, None)
        assert channel._closed is True


# ---------------------------------------------------------------------------
# Transport tests
# ---------------------------------------------------------------------------


class TestConsumerRouting:
    async def test_each_consumer_on_a_queue_gets_only_its_own_deliveries(self, channel, aio_channel):
        aio_channel.declare_queue = AsyncMock(return_value=_make_aio_queue("q1"))
        await channel.declare_queue(Queue("q1", durable=True))
        first: list[Message] = []
        second: list[Message] = []
        await channel.basic_consume("q1", lambda b, m: first.append(m), consumer_tag="tag1")
        await channel.basic_consume("q1", lambda b, m: second.append(m), consumer_tag="tag2")

        message = _make_kombu_message(channel, consumer_tag="tag2")
        await channel._message_queue.put(("q1", message))

        assert await channel.drain_events(timeout=0) is True
        assert first == []
        assert second == [message]

    async def test_a_delivery_without_a_known_tag_goes_to_the_queue_consumer(self, channel):
        received: list[Message] = []
        channel._consumers["tag1"] = ("q1", lambda b, m: received.append(m), False)

        message = _make_kombu_message(channel, consumer_tag="gone")
        await channel._message_queue.put(("q1", message))

        assert await channel.drain_events(timeout=0) is True
        assert received == [message]


class TestChannelRecovery:
    async def test_a_channel_the_broker_closed_is_reopened(self, channel, aio_channel):
        aio_queue = _make_aio_queue("q1")
        aio_channel.declare_queue = AsyncMock(return_value=aio_queue)
        await channel.declare_queue(Queue("q1", durable=True))
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")
        aio_queue.consume.reset_mock()

        aio_channel.is_closed = True
        aio_channel.close_callbacks.fire(aio_channel, aiormq_exc.ChannelClosed(404, "gone"))
        aio_channel.is_closed = True

        await channel.declare_exchange(Exchange("ex1", type="direct"))

        aio_channel.reopen.assert_awaited_once()
        assert aio_channel.declare_queue.await_count == 2
        aio_queue.consume.assert_awaited_once()

    async def test_a_reopened_channel_drops_what_the_broker_will_send_again(self, channel, aio_channel):
        """An unacknowledged delivery goes back on the queue when a channel closes.

        Keeping the buffered copy would run the same message twice.
        """
        unacked = _make_kombu_message(channel, delivery_tag="1")
        no_ack = _make_kombu_message(channel, delivery_tag="2")
        channel._delivery_tag_map["1"] = MagicMock()
        await channel._message_queue.put(("q1", unacked))
        await channel._message_queue.put(("q1", no_ack))
        aio_channel.is_closed = True

        received = []
        channel._consumers["tag1"] = ("q1", lambda body, message: received.append(message), False)

        assert await channel.drain_events(timeout=0) is True
        assert received == [no_ack]
        assert await channel.drain_events(timeout=0) is False

    async def test_a_reopened_channel_forgets_its_delivery_tags(self, channel, aio_channel):
        channel._delivery_tag_map["1"] = MagicMock()
        aio_channel.is_closed = True

        await channel.drain_events(timeout=0)

        assert channel._delivery_tag_map == {}

    async def test_a_lost_connection_is_reported_once_and_then_repaired(self):
        transport = Transport(url="amqp://localhost/")
        old_aio_channel = _make_aio_channel()
        new_aio_channel = _make_aio_channel()
        transport.new_aio_channel = AsyncMock(return_value=new_aio_channel)

        channel = Channel(old_aio_channel, transport=transport)
        aio_queue = _make_aio_queue("q1")
        old_aio_channel.declare_queue = AsyncMock(return_value=aio_queue)
        await channel.declare_queue(Queue("q1", durable=True))
        await channel.basic_consume("q1", MagicMock(), consumer_tag="tag1")

        restored_queue = _make_aio_queue("q1")
        new_aio_channel.declare_queue = AsyncMock(return_value=restored_queue)
        channel.on_connection_closed(ConnectionResetError("broker went away"))

        with pytest.raises(Transport.connection_errors):
            await channel.drain_events(timeout=0)

        await channel.drain_events(timeout=0)

        assert channel._aio_channel is new_aio_channel
        assert channel._declared_queues["q1"] is restored_queue
        restored_queue.consume.assert_awaited_once()

    async def test_a_channel_without_a_transport_keeps_reporting_the_loss(self, channel):
        channel.on_connection_closed(ConnectionResetError("broker went away"))

        with pytest.raises(Transport.connection_errors):
            await channel.drain_events(timeout=0)
        with pytest.raises(Transport.connection_errors):
            await channel.drain_events(timeout=0)

    async def test_bindings_are_restored_with_the_queue(self, channel, aio_channel):
        aio_queue = _make_aio_queue("q1")
        aio_channel.declare_queue = AsyncMock(return_value=aio_queue)
        aio_exchange = _make_aio_exchange("ex1")
        aio_channel.get_exchange = AsyncMock(return_value=aio_exchange)
        await channel.declare_queue(Queue("q1", durable=True))
        await channel.queue_bind("q1", "ex1", routing_key="rk")
        aio_queue.bind.reset_mock()

        aio_channel.is_closed = True
        await channel.drain_events(timeout=0)

        aio_queue.bind.assert_awaited_once_with(aio_exchange, routing_key="rk", arguments=None)

    async def test_an_unbound_queue_is_not_bound_again_after_a_reopen(self, channel, aio_channel):
        aio_queue = _make_aio_queue("q1")
        aio_channel.declare_queue = AsyncMock(return_value=aio_queue)
        aio_channel.get_exchange = AsyncMock(return_value=_make_aio_exchange("ex1"))
        await channel.declare_queue(Queue("q1", durable=True))
        await channel.queue_bind("q1", "ex1", routing_key="rk")
        await channel.queue_unbind("q1", "ex1", routing_key="rk")
        aio_queue.bind.reset_mock()

        aio_channel.is_closed = True
        await channel.drain_events(timeout=0)

        aio_queue.bind.assert_not_awaited()

    async def test_binding_a_queue_reopens_a_channel_the_broker_closed(self, channel, aio_channel):
        aio_queue = _make_aio_queue("q1")
        aio_channel.declare_queue = AsyncMock(return_value=aio_queue)
        aio_exchange = _make_aio_exchange("ex1")
        aio_channel.get_exchange = AsyncMock(return_value=aio_exchange)
        await channel.declare_queue(Queue("q1", durable=True))

        aio_channel.is_closed = True
        await channel.queue_bind("q1", "ex1", routing_key="rk")

        aio_channel.reopen.assert_awaited_once()
        aio_queue.bind.assert_awaited_once_with(aio_exchange, routing_key="rk", arguments=None)

    async def test_a_server_named_queue_is_redeclared_under_its_given_name(self, channel, aio_channel):
        aio_channel.declare_queue = AsyncMock(return_value=_make_aio_queue("amq.gen-abc"))
        await channel.declare_queue(Queue("", durable=False, exclusive=True))

        aio_channel.is_closed = True
        await channel.drain_events(timeout=0)

        assert aio_channel.declare_queue.await_args_list[0].kwargs["name"] is None
        assert aio_channel.declare_queue.await_args_list[1].kwargs["name"] == "amq.gen-abc"


class TestTransport:
    def test_class_attributes(self):
        assert Transport.default_port == 5672
        assert Transport.driver_type == "amqp"
        assert Transport.driver_name == "aio-pika"
        assert Transport.Channel is Channel

    def test_init(self):
        t = Transport(url="amqp://guest:guest@localhost/")
        assert t._url == "amqp://guest:guest@localhost/"
        assert t._connection is None
        assert list(t._channels) == []
        assert t.is_connected is False

    def test_init_stores_options(self):
        t = Transport(url="amqp://localhost/", prefetch_count=10, heartbeat=30)
        assert t._options["prefetch_count"] == 10
        assert t._options["heartbeat"] == 30

    def test_error_tuples(self):
        from aiormq import exceptions as aiormq_exc

        assert ConnectionRefusedError in Transport.connection_errors
        assert ConnectionResetError in Transport.connection_errors
        assert TimeoutError in Transport.connection_errors
        assert OSError in Transport.connection_errors
        assert aiormq_exc.AMQPConnectionError in Transport.connection_errors
        assert aiormq_exc.AMQPChannelError in Transport.channel_errors

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://guest:guest@rabbit:5672/")
        await t.connect()

        assert t.is_connected is True
        assert t._connection is mock_conn
        mock_aio_pika.connect.assert_awaited_once_with("amqp://guest:guest@rabbit:5672/")

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_idempotent(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        await t.connect()

        assert mock_aio_pika.connect.await_count == 1

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_with_heartbeat(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/", heartbeat=60)
        await t.connect()

        mock_aio_pika.connect.assert_awaited_once_with("amqp://localhost/?heartbeat=60")

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_with_a_connection_timeout(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())

        t = Transport(url="amqp://localhost/", connection_timeout=5)
        await t.connect()

        mock_aio_pika.connect.assert_awaited_once_with("amqp://localhost/?timeout=5", timeout=5.0)

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_with_tls_files(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())

        t = Transport(
            url="amqp://localhost/",
            ssl={"ca_certs": "/ca.pem", "certfile": "/c.pem", "keyfile": "/k.pem", "cert_reqs": ssl.CERT_NONE},
        )
        await t.connect()

        url = mock_aio_pika.connect.await_args.args[0]
        assert url.startswith("amqps://localhost/?")
        assert dict(parse_qsl(urlsplit(url).query)) == {
            "cafile": "/ca.pem",
            "certfile": "/c.pem",
            "keyfile": "/k.pem",
            "no_verify_ssl": "1",
        }

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_with_an_ssl_context(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())
        context = ssl.create_default_context()

        t = Transport(url="amqp://localhost/", ssl=context)
        await t.connect()

        mock_aio_pika.connect.assert_awaited_once_with("amqps://localhost/", ssl_context=context)

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_keeps_what_the_url_already_says(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())

        t = Transport(url="amqp://guest:guest@localhost:5672//?name=worker-1", heartbeat=60)
        await t.connect()

        url = mock_aio_pika.connect.await_args.args[0]
        assert url.startswith("amqp://guest:guest@localhost:5672//?")
        assert dict(parse_qsl(urlsplit(url).query)) == {"name": "worker-1", "heartbeat": "60"}

    @patch("kombu.transport.amqp.aio_pika")
    async def test_close(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        await t.close()

        assert t.is_connected is False
        assert t._connection is None
        mock_conn.close.assert_awaited_once()

    @patch("kombu.transport.amqp.aio_pika")
    async def test_close_closes_channels(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_ch = _make_aio_channel()
        mock_conn.channel = AsyncMock(return_value=mock_aio_ch)
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        ch = await t.create_channel()
        await t.close()

        assert ch._closed is True
        assert list(t._channels) == []

    @patch("kombu.transport.amqp.aio_pika")
    async def test_create_channel(self, mock_aio_pika):
        mock_aio_ch = _make_aio_channel()
        mock_conn = _make_aio_connection()
        mock_conn.channel = AsyncMock(return_value=mock_aio_ch)
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        ch = await t.create_channel()

        assert isinstance(ch, Channel)
        assert ch._aio_channel is mock_aio_ch
        assert ch in t._channels
        mock_conn.channel.assert_awaited_once_with(publisher_confirms=True)

    @patch("kombu.transport.amqp.aio_pika")
    async def test_create_channel_with_prefetch(self, mock_aio_pika):
        mock_aio_ch = _make_aio_channel()
        mock_conn = _make_aio_connection()
        mock_conn.channel = AsyncMock(return_value=mock_aio_ch)
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/", prefetch_count=10)
        await t.connect()
        channel = await t.create_channel()

        mock_aio_ch.set_qos.assert_awaited_once_with(prefetch_count=10)
        assert channel._message_queue.maxsize == 10

    @patch("kombu.transport.amqp.aio_pika")
    async def test_create_channel_no_publisher_confirms(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/", publisher_confirms=False)
        await t.connect()
        await t.create_channel()

        mock_conn.channel.assert_awaited_once_with(publisher_confirms=False)

    def test_is_connected_false_by_default(self):
        t = Transport(url="amqp://localhost/")
        assert t.is_connected is False

    @patch("kombu.transport.amqp.aio_pika")
    async def test_is_connected_true_after_connect(self, mock_aio_pika):
        mock_aio_pika.connect = AsyncMock(return_value=_make_aio_connection())
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        assert t.is_connected is True

    @patch("kombu.transport.amqp.aio_pika")
    async def test_is_connected_false_when_connection_closed(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        mock_conn.is_closed = True
        assert t.is_connected is False

    def test_driver_version(self):
        t = Transport(url="amqp://localhost/")
        version = t.driver_version()
        assert version  # aio-pika is installed, so we get a real version

    async def test_close_when_not_connected(self):
        t = Transport(url="amqp://localhost/")
        # Should not raise
        await t.close()
        assert t.is_connected is False

    @patch("kombu.transport.amqp.aio_pika")
    async def test_close_when_connection_already_closed(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        mock_conn.is_closed = True
        await t.close()

        mock_conn.close.assert_not_awaited()

    @patch("kombu.transport.amqp.aio_pika")
    async def test_create_channel_auto_connects(self, mock_aio_pika):
        mock_aio_ch = _make_aio_channel()
        mock_conn = _make_aio_connection()
        mock_conn.channel = AsyncMock(return_value=mock_aio_ch)
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        assert t.is_connected is False

        ch = await t.create_channel()

        assert t.is_connected is True
        assert isinstance(ch, Channel)

    @patch("kombu.transport.amqp.aio_pika")
    async def test_create_channel_no_prefetch_by_default(self, mock_aio_pika):
        mock_aio_ch = _make_aio_channel()
        mock_conn = _make_aio_connection()
        mock_conn.channel = AsyncMock(return_value=mock_aio_ch)
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)
        mock_aio_pika.Message = aio_pika.Message
        mock_aio_pika.DeliveryMode = aio_pika.DeliveryMode
        mock_aio_pika.ExchangeType = aio_pika.ExchangeType

        t = Transport(url="amqp://localhost/")
        await t.connect()
        await t.create_channel()

        mock_aio_ch.set_qos.assert_not_awaited()

    def test_exchange_types_attribute(self):
        assert Transport.exchange_types == {"direct", "fanout", "topic", "headers"}

    def test_no_duplicate_connection_errors(self):
        errors = Transport.connection_errors
        assert len(errors) == len(set(errors))

    def test_driver_version_missing_attr(self):
        t = Transport(url="amqp://localhost/")
        with patch("kombu.transport.amqp.aio_pika") as mock_mod:
            del mock_mod.__version__
            assert t.driver_version() == "N/A"

    @patch("kombu.transport.amqp.aio_pika")
    async def test_is_connected_is_false_after_a_server_side_close(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)

        t = Transport(url="amqp://localhost/")
        await t.connect()
        mock_conn.connected.clear()

        assert mock_conn.is_closed is False
        assert t.is_connected is False

    @patch("kombu.transport.amqp.aio_pika")
    async def test_connect_replaces_a_connection_the_broker_closed(self, mock_aio_pika):
        dead = _make_aio_connection()
        alive = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(side_effect=[dead, alive])

        t = Transport(url="amqp://localhost/")
        await t.connect()
        dead.connected.clear()
        await t.connect()

        assert t._connection is alive
        assert t.is_connected is True
        assert mock_aio_pika.connect.await_count == 2

    @patch("kombu.transport.amqp.aio_pika")
    async def test_a_closed_connection_reaches_its_channels(self, mock_aio_pika):
        mock_conn = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(return_value=mock_conn)

        t = Transport(url="amqp://localhost/")
        ch = await t.create_channel()
        mock_conn.close_callbacks.fire(mock_conn, ConnectionResetError("broker went away"))

        with pytest.raises(Transport.connection_errors):
            await ch.drain_events(timeout=0)

    @patch("kombu.transport.amqp.aio_pika")
    async def test_a_close_callback_from_a_replaced_connection_is_ignored(self, mock_aio_pika):
        dead = _make_aio_connection()
        alive = _make_aio_connection()
        mock_aio_pika.connect = AsyncMock(side_effect=[dead, alive])

        t = Transport(url="amqp://localhost/")
        await t.connect()
        dead.connected.clear()
        await t.connect()
        ch = await t.create_channel()

        dead.close_callbacks.fire(dead, ConnectionResetError("late"))

        assert await ch.drain_events(timeout=0) is False


# ---------------------------------------------------------------------------
# Connection registration
# ---------------------------------------------------------------------------


class TestConnectionRegistration:
    def test_amqp_scheme(self):
        from kombu.connection import _get_transport_class

        cls = _get_transport_class("amqp")
        assert cls is Transport

    def test_amqps_scheme(self):
        from kombu.connection import _get_transport_class

        cls = _get_transport_class("amqps")
        assert cls is Transport

    def test_connection_object(self):
        from kombu import Connection

        conn = Connection("amqp://guest:guest@localhost/")
        info = conn.info()
        assert info["transport"] == "amqp"


# ---------------------------------------------------------------------------
# End-to-end flow with mocked aio-pika (no broker)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_publish_consume_roundtrip(self, channel):
        """Simulate full publish → consume → ack flow with mocked aio-pika."""
        # Set up queue
        aio_q = _make_aio_queue("tasks")
        channel._declared_queues["tasks"] = aio_q

        # Register consumer
        received = []

        async def on_message(body, message):
            received.append(body)
            await message.ack()

        await channel.basic_consume("tasks", on_message, consumer_tag="worker", no_ack=False)

        # Extract internal callback
        internal_cb = aio_q.consume.call_args[1]["callback"]

        # Simulate broker delivering a message
        incoming = _make_incoming_message(
            body=b'[[2, 3], {}, {"callbacks": null}]',
            delivery_tag=1,
            content_type="application/json",
            content_encoding="utf-8",
            headers={"task": "myapp.add", "id": "task-001"},
            correlation_id="task-001",
        )
        await internal_cb(incoming)

        # drain_events should pick it up and deliver
        result = await channel.drain_events(timeout=1.0)

        assert result is True
        assert len(received) == 1
        assert received[0] == [[2, 3], {}, {"callbacks": None}]
        # After ack, delivery tag is removed
        assert "1" not in channel._delivery_tag_map
        incoming.ack.assert_awaited_once()

    async def test_publish_reject_requeue(self, channel):
        """Simulate publish → consume → reject(requeue=True)."""
        aio_q = _make_aio_queue("tasks")
        channel._declared_queues["tasks"] = aio_q

        rejected = []

        async def on_message(body, message):
            rejected.append(body)
            await message.reject(requeue=True)

        await channel.basic_consume("tasks", on_message, consumer_tag="worker")

        internal_cb = aio_q.consume.call_args[1]["callback"]
        incoming = _make_incoming_message(delivery_tag=5)
        await internal_cb(incoming)

        await channel.drain_events(timeout=1.0)

        assert len(rejected) == 1
        incoming.reject.assert_awaited_once_with(requeue=True)
        assert "5" not in channel._delivery_tag_map

    async def test_multiple_consumers_routing(self, channel):
        """Messages are routed to the correct consumer by queue name."""
        q1 = _make_aio_queue("q1")
        q2 = _make_aio_queue("q2")
        channel._declared_queues["q1"] = q1
        channel._declared_queues["q2"] = q2

        received_q1 = []
        received_q2 = []

        await channel.basic_consume("q1", lambda b, m: received_q1.append(b), consumer_tag="c1")
        await channel.basic_consume("q2", lambda b, m: received_q2.append(b), consumer_tag="c2")

        # Simulate message to q2
        cb2 = q2.consume.call_args[1]["callback"]
        incoming = _make_incoming_message(body=b'"for q2"', delivery_tag=10)
        await cb2(incoming)
        await channel.drain_events(timeout=1.0)

        assert received_q1 == []
        assert received_q2 == ["for q2"]
