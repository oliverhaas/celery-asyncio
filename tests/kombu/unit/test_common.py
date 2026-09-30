"""Tests for kombu.common - common utilities."""

import pytest

from kombu import Connection, Exchange, Queue
from kombu.common import Broadcast, QoS, eventloop, maybe_declare
from tests.kombu.mocks import MockChannel


class test_Broadcast:
    """Tests for Broadcast queue."""

    def test_default(self):
        b = Broadcast("test_broadcast")
        assert b.exchange.name == "test_broadcast"
        assert b.exchange.type == "fanout"
        assert b.auto_delete is True
        # Name is auto-generated
        assert b.name.startswith("bcast.")

    def test_custom_queue(self):
        b = Broadcast("test", queue="myqueue")
        assert b.name == "myqueue"

    def test_unique(self):
        b1 = Broadcast("test", unique=True)
        b2 = Broadcast("test", unique=True)
        assert b1.name != b2.name  # Different unique names

    def test_custom_exchange(self):
        ex = Exchange("custom", type="direct")
        b = Broadcast("test", exchange=ex)
        assert b.exchange is ex


class test_maybe_declare:
    """Tests for maybe_declare."""

    async def test_declare_exchange(self, mock_channel):
        ex = Exchange("test")
        result = await maybe_declare(ex, mock_channel)
        assert result is True
        assert any(c[0] == "declare_exchange" for c in mock_channel.calls)

    async def test_declare_queue(self, mock_channel):
        ex = Exchange("test")
        q = Queue("test_q", exchange=ex, routing_key="rk")
        result = await maybe_declare(q, mock_channel)
        assert result is True
        # Should declare exchange, declare queue, and bind
        assert any(c[0] == "declare_exchange" for c in mock_channel.calls)
        assert any(c[0] == "declare_queue" for c in mock_channel.calls)
        assert any(c[0] == "queue_bind" for c in mock_channel.calls)

    async def test_declare_queue_no_exchange(self, mock_channel):
        q = Queue("test_q")
        result = await maybe_declare(q, mock_channel)
        assert result is True
        assert any(c[0] == "declare_queue" for c in mock_channel.calls)

    async def test_no_channel_raises(self):
        ex = Exchange("test")
        with pytest.raises(ValueError, match="Channel is required"):
            await maybe_declare(ex, None)

    async def test_declares_once_per_channel(self, mock_channel):
        # A publisher declares its target queue before every send, and every
        # declare after the first cost broker round trips to learn nothing.
        q = Queue("test_q", exchange=Exchange("test"), routing_key="rk")
        assert await maybe_declare(q, mock_channel) is True
        assert await maybe_declare(q, mock_channel) is False
        assert [c[0] for c in mock_channel.calls].count("declare_queue") == 1
        assert [c[0] for c in mock_channel.calls].count("queue_bind") == 1

    async def test_a_new_channel_declares_again(self, mock_channel, mock_transport):
        # A reconnect opens a new channel, and what the old one declared may
        # have gone with the broker it was declared on.
        q = Queue("test_q")
        await maybe_declare(q, mock_channel)
        other = MockChannel(transport=mock_transport)
        assert await maybe_declare(q, other) is True
        assert any(c[0] == "declare_queue" for c in other.calls)

    @pytest.mark.parametrize(
        "make_queue",
        [
            pytest.param(lambda: Queue("test_q", auto_delete=True), id="auto_delete"),
            pytest.param(lambda: Queue("test_q", expires=60), id="x-expires"),
            pytest.param(lambda: Queue("test_q", exchange=Exchange("ex", auto_delete=True)), id="auto_delete_exchange"),
        ],
    )
    async def test_what_the_broker_can_drop_is_declared_every_time(self, mock_channel, make_queue):
        q = make_queue()
        assert await maybe_declare(q, mock_channel) is True
        assert await maybe_declare(q, mock_channel) is True
        assert [c[0] for c in mock_channel.calls].count("declare_queue") == 2

    async def test_an_auto_delete_exchange_is_declared_every_time(self, mock_channel):
        ex = Exchange("test", auto_delete=True)
        await maybe_declare(ex, mock_channel)
        assert await maybe_declare(ex, mock_channel) is True
        assert [c[0] for c in mock_channel.calls].count("declare_exchange") == 2

    async def test_the_same_name_bound_another_way_is_declared(self, mock_channel):
        ex = Exchange("test")
        await maybe_declare(Queue("test_q", exchange=ex, routing_key="a"), mock_channel)
        assert await maybe_declare(Queue("test_q", exchange=ex, routing_key="b"), mock_channel) is True
        bound = [c[1][2] for c in mock_channel.calls if c[0] == "queue_bind"]
        assert bound == ["a", "b"]

    async def test_an_equal_queue_object_is_not_declared_again(self, mock_channel):
        await maybe_declare(Queue("test_q", exchange=Exchange("test")), mock_channel)
        assert await maybe_declare(Queue("test_q", exchange=Exchange("test")), mock_channel) is False


class test_eventloop:
    """Tests for eventloop async generator."""

    async def test_with_limit(self):
        async with Connection("memory://") as conn:
            count = 0
            async for _ in eventloop(conn, limit=3, timeout=0.01, ignore_timeouts=True):
                count += 1
            assert count == 3

    async def test_ignore_timeouts(self):
        async with Connection("memory://") as conn:
            count = 0
            async for _ in eventloop(conn, limit=2, timeout=0.01, ignore_timeouts=True):
                count += 1
            assert count == 2

    async def test_timeout_raises(self):
        async with Connection("memory://") as conn:
            with pytest.raises(TimeoutError):
                async for _ in eventloop(conn, limit=1, timeout=0.01, ignore_timeouts=False):
                    pass


class test_QoS:
    """Tests for QoS class."""

    def test_init(self):
        def callback(**kwargs):
            pass

        qos = QoS(callback, initial_value=10)
        assert qos.value == 10
        assert qos.prev is None

    def test_increment(self):
        qos = QoS(lambda **kwargs: None, initial_value=10)
        result = qos.increment_eventually(3)
        assert result == 13

    def test_increment_negative(self):
        qos = QoS(lambda **kwargs: None, initial_value=10)
        result = qos.increment_eventually(-1)
        assert result == 10  # max(n, 0) means negative increments are 0

    def test_increment_with_max(self):
        qos = QoS(lambda **kwargs: None, initial_value=10, max_prefetch=15)
        qos.increment_eventually(10)
        assert qos.value == 15  # Capped at max

    def test_decrement(self):
        qos = QoS(lambda **kwargs: None, initial_value=10)
        result = qos.decrement_eventually(3)
        assert result == 7

    def test_decrement_floor(self):
        qos = QoS(lambda **kwargs: None, initial_value=3)
        result = qos.decrement_eventually(10)
        assert result == 1  # Floor is 1

    async def test_update(self):
        called_with = {}

        def callback(**kwargs):
            called_with.update(kwargs)

        qos = QoS(callback, initial_value=10)
        await qos.update()
        assert called_with["prefetch_count"] == 10
        assert qos.prev == 10

    async def test_update_no_change(self):
        call_count = 0

        def callback(**kwargs):
            nonlocal call_count
            call_count += 1

        qos = QoS(callback, initial_value=10)
        await qos.update()
        assert call_count == 1
        await qos.update()
        assert call_count == 1  # Not called again, same value

    async def test_set(self):
        called_with = {}

        def callback(**kwargs):
            called_with.update(kwargs)

        qos = QoS(callback, initial_value=10)
        await qos.set(20)
        assert called_with["prefetch_count"] == 20
        assert qos.prev == 20
