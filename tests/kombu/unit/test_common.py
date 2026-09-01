"""Tests for kombu.common - common utilities."""

import pytest

from kombu import Connection, Exchange, Queue
from kombu.common import Broadcast, QoS, eventloop, maybe_declare


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
