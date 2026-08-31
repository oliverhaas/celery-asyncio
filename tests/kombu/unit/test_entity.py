"""Tests for kombu.entity - Exchange, Queue, binding."""

from kombu.entity import (
    PERSISTENT_DELIVERY_MODE,
    TRANSIENT_DELIVERY_MODE,
    Exchange,
    Queue,
    binding,
    maybe_delivery_mode,
)


class test_Exchange:
    """Tests for Exchange class."""

    def test_defaults(self):
        ex = Exchange("test")
        assert ex.name == "test"
        assert ex.type == "direct"
        assert ex.durable is True
        assert ex.auto_delete is False
        assert ex.delivery_mode is None
        assert ex.arguments == {}
        assert ex.no_declare is False

    def test_fanout(self):
        ex = Exchange("fanout_test", type="fanout")
        assert ex.type == "fanout"

    def test_topic(self):
        ex = Exchange("topic_test", type="topic")
        assert ex.type == "topic"

    def test_delivery_mode(self):
        ex = Exchange("test", delivery_mode="transient")
        assert ex.delivery_mode == TRANSIENT_DELIVERY_MODE

        ex = Exchange("test", delivery_mode="persistent")
        assert ex.delivery_mode == PERSISTENT_DELIVERY_MODE

    def test_hash(self):
        ex1 = Exchange("test")
        ex2 = Exchange("test")
        assert hash(ex1) == hash(ex2)
        assert hash(ex1) != hash(Exchange("other"))

    def test_eq(self):
        ex1 = Exchange("test", type="direct")
        ex2 = Exchange("test", type="direct")
        assert ex1 == ex2

    def test_eq_different_name(self):
        assert Exchange("a") != Exchange("b")

    def test_eq_different_type(self):
        assert Exchange("a", type="direct") != Exchange("a", type="fanout")

    def test_eq_not_exchange(self):
        assert Exchange("test").__eq__("not_exchange") is NotImplemented

    def test_repr(self):
        ex = Exchange("test", type="direct")
        assert "test" in repr(ex)
        assert "direct" in repr(ex)

    def test_str(self):
        ex = Exchange("test", type="direct")
        s = str(ex)
        assert "test" in s
        assert "direct" in s

    async def test_declare(self, mock_channel):
        ex = Exchange("test")
        await ex.declare(mock_channel)
        assert any(c[0] == "declare_exchange" for c in mock_channel.calls)

    async def test_declare_no_declare(self, mock_channel):
        ex = Exchange("test", no_declare=True)
        await ex.declare(mock_channel)
        assert not any(c[0] == "declare_exchange" for c in mock_channel.calls)

    async def test_declare_empty_name(self, mock_channel):
        ex = Exchange("")
        await ex.declare(mock_channel)
        # Empty name exchange still goes through declare (transport decides)
        assert any(c[0] == "declare_exchange" for c in mock_channel.calls)

    def test_bind_channel(self):
        ex = Exchange("test")
        ch = object()
        result = ex.bind(ch)
        assert result is ex
        assert ex._channel is ch


class test_Queue:
    """Tests for Queue class."""

    def test_defaults(self):
        q = Queue("test")
        assert q.name == "test"
        assert q.exchange is None
        assert q.routing_key == "test"  # Defaults to name
        assert q.durable is True
        assert q.exclusive is False
        assert q.auto_delete is False
        assert q.no_ack is False

    def test_with_exchange(self):
        ex = Exchange("myex", type="direct")
        q = Queue("myq", exchange=ex, routing_key="rk")
        assert q.exchange is ex
        assert q.routing_key == "rk"

    def test_exchange_string(self):
        q = Queue("myq", exchange="myex")
        assert isinstance(q.exchange, Exchange)
        assert q.exchange.name == "myex"

    def test_exchange_empty_string(self):
        q = Queue("myq", exchange="")
        assert q.exchange is None

    def test_routing_key_defaults_to_name(self):
        q = Queue("myq")
        assert q.routing_key == "myq"

    def test_routing_key_explicit(self):
        q = Queue("myq", routing_key="custom")
        assert q.routing_key == "custom"

    def test_queue_arguments(self):
        q = Queue("test", expires=60, message_ttl=30, max_length=100)
        assert q.queue_arguments["x-expires"] == 60000
        assert q.queue_arguments["x-message-ttl"] == 30000
        assert q.queue_arguments["x-max-length"] == 100

    def test_max_priority(self):
        q = Queue("test", max_priority=10)
        assert q.queue_arguments["x-max-priority"] == 10

    def test_hash(self):
        q1 = Queue("test")
        q2 = Queue("test")
        assert hash(q1) == hash(q2)

    def test_eq(self):
        assert Queue("test") == Queue("test")

    def test_eq_different_name(self):
        assert Queue("a") != Queue("b")

    def test_eq_not_queue(self):
        assert Queue("test").__eq__("not_queue") is NotImplemented

    def test_repr(self):
        assert "test" in repr(Queue("test"))

    def test_str(self):
        assert "test" in str(Queue("test"))

    async def test_declare(self, mock_channel):
        q = Queue("test")
        name = await q.declare(mock_channel)
        assert name == "test"
        assert any(c[0] == "declare_queue" for c in mock_channel.calls)

    async def test_declare_no_declare(self, mock_channel):
        q = Queue("test", no_declare=True)
        name = await q.declare(mock_channel)
        assert name == "test"
        assert not any(c[0] == "declare_queue" for c in mock_channel.calls)

    async def test_bind(self, mock_channel):
        ex = Exchange("myex")
        q = Queue("test", exchange=ex, routing_key="rk")
        await q.bind(mock_channel)
        assert any(c[0] == "queue_bind" for c in mock_channel.calls)

    async def test_bind_no_exchange(self, mock_channel):
        q = Queue("test")
        await q.bind(mock_channel)
        # No binding if no exchange
        assert not any(c[0] == "queue_bind" for c in mock_channel.calls)

    async def test_get(self, mock_channel):
        q = Queue("test")
        result = await q.get(mock_channel)
        assert result is None  # Empty queue
        assert any(c[0] == "get" for c in mock_channel.calls)

    async def test_purge(self, mock_channel):
        q = Queue("test")
        count = await q.purge(mock_channel)
        assert count == 0
        assert any(c[0] == "queue_purge" for c in mock_channel.calls)

    async def test_delete(self, mock_channel):
        q = Queue("test")
        count = await q.delete(mock_channel)
        assert count == 0
        assert any(c[0] == "queue_delete" for c in mock_channel.calls)

    def test_bind_to_channel(self):
        q = Queue("test")
        ch = object()
        result = q.bind_to_channel(ch)
        assert result is q
        assert q._channel is ch


class test_binding:
    """Tests for binding class."""

    def test_init(self):
        ex = Exchange("test")
        b = binding(exchange=ex, routing_key="rk")
        assert b.exchange is ex
        assert b.routing_key == "rk"

    def test_repr(self):
        b = binding(exchange=Exchange("test"), routing_key="rk")
        assert "test" in repr(b)

    def test_str(self):
        b = binding(exchange=Exchange("test"), routing_key="rk")
        s = str(b)
        assert "rk" in s

    async def test_declare(self, mock_channel):
        ex = Exchange("test")
        b = binding(exchange=ex)
        await b.declare(mock_channel)
        assert any(c[0] == "declare_exchange" for c in mock_channel.calls)

    async def test_bind_queue(self, mock_channel):
        ex = Exchange("test")
        b = binding(exchange=ex, routing_key="rk")
        q = Queue("myq")
        q._channel = mock_channel
        await b.bind(q, mock_channel)
        assert any(c[0] == "queue_bind" for c in mock_channel.calls)

    async def test_unbind_queue(self, mock_channel):
        ex = Exchange("test")
        b = binding(exchange=ex, routing_key="rk")
        q = Queue("myq")
        q._channel = mock_channel
        await b.unbind(q, mock_channel)
        assert any(c[0] == "queue_unbind" for c in mock_channel.calls)


class test_maybe_delivery_mode:
    """Tests for maybe_delivery_mode utility."""

    def test_none(self):
        assert maybe_delivery_mode(None) == PERSISTENT_DELIVERY_MODE

    def test_string(self):
        assert maybe_delivery_mode("transient") == TRANSIENT_DELIVERY_MODE
        assert maybe_delivery_mode("persistent") == PERSISTENT_DELIVERY_MODE

    def test_int(self):
        assert maybe_delivery_mode(1) == 1
        assert maybe_delivery_mode(2) == 2
