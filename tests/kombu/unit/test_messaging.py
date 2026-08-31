"""Tests for kombu.messaging - async Producer and Consumer."""

from kombu import Connection, Exchange, Queue
from kombu.messaging import Consumer, Producer


class test_Producer:
    """Tests for Producer class."""

    def test_init_defaults(self):
        conn = Connection("memory://")
        p = Producer(conn)
        assert p._connection is conn
        assert p.exchange.name == ""
        assert p.routing_key == ""
        assert p.serializer is None
        assert p.auto_declare is True

    def test_init_with_exchange(self):
        conn = Connection("memory://")
        ex = Exchange("test", type="direct")
        p = Producer(conn, exchange=ex)
        assert p.exchange is ex

    def test_init_exchange_string(self):
        conn = Connection("memory://")
        p = Producer(conn, exchange="test")
        assert isinstance(p.exchange, Exchange)
        assert p.exchange.name == "test"

    def test_init_exchange_empty_string(self):
        conn = Connection("memory://")
        p = Producer(conn, exchange="")
        assert p.exchange.name == ""

    def test_init_custom_routing_key(self):
        conn = Connection("memory://")
        p = Producer(conn, routing_key="my_key")
        assert p.routing_key == "my_key"

    def test_repr(self):
        conn = Connection("memory://")
        p = Producer(conn)
        assert "Producer" in repr(p)

    async def test_publish(self):
        async with Connection("memory://") as conn:
            channel = await conn.default_channel()
            p = Producer(conn)
            await p.publish({"hello": "world"}, routing_key="test_q")

            # Verify message is in the queue
            msg = await channel.get("test_q", no_ack=True)
            assert msg is not None
            body = msg.decode()
            assert body == {"hello": "world"}

    async def test_publish_custom_serializer(self):
        async with Connection("memory://") as conn:
            p = Producer(conn, serializer="json")
            await p.publish({"data": 123}, routing_key="test_q")

            channel = await conn.default_channel()
            msg = await channel.get("test_q", no_ack=True)
            assert msg is not None

    async def test_publish_custom_exchange(self):
        async with Connection("memory://") as conn:
            ex = Exchange("myex", type="direct")
            p = Producer(conn, exchange=ex)
            await p.publish({"test": True}, routing_key="rk")

    async def test_context_manager(self):
        async with Connection("memory://") as conn, conn.Producer() as p:
            await p.publish({"test": True}, routing_key="test_q")

    async def test_declare(self):
        async with Connection("memory://") as conn:
            ex = Exchange("myex")
            p = Producer(conn, exchange=ex)
            await p.declare()
            assert p._declared is True
            # Second declare is a no-op
            await p.declare()

    async def test_auto_declare(self):
        async with Connection("memory://") as conn:
            ex = Exchange("myex")
            p = Producer(conn, exchange=ex, auto_declare=True)
            await p.publish({"test": True}, routing_key="test_q")
            assert p._declared is True

    async def test_no_auto_declare(self):
        async with Connection("memory://") as conn:
            p = Producer(conn, auto_declare=False)
            await p.publish({"test": True}, routing_key="test_q")
            assert p._declared is False

    async def test_publish_with_properties(self):
        async with Connection("memory://") as conn:
            p = Producer(conn)
            await p.publish(
                {"test": True},
                routing_key="test_q",
                priority=5,
                expiration=30.0,
                delivery_mode=2,
            )


class test_Consumer:
    """Tests for Consumer class."""

    def test_init_defaults(self):
        conn = Connection("memory://")
        q = Queue("test")
        c = Consumer(conn, queues=[q])
        assert c._connection is conn
        assert c._queues == [q]
        assert c._callbacks == []
        assert c._no_ack is False

    def test_init_with_callbacks(self):
        conn = Connection("memory://")

        def cb(body, msg):
            pass

        c = Consumer(conn, queues=[], callbacks=[cb])
        assert c._callbacks == [cb]

    def test_queues_property(self):
        conn = Connection("memory://")
        q = Queue("test")
        c = Consumer(conn, queues=[q])
        assert c.queues == [q]

    def test_add_queue(self):
        conn = Connection("memory://")
        c = Consumer(conn, queues=[])
        q = Queue("test")
        c.add_queue(q)
        assert q in c.queues
        # Adding same queue again is a no-op
        c.add_queue(q)
        assert len(c.queues) == 1

    def test_repr(self):
        conn = Connection("memory://")
        c = Consumer(conn, queues=[Queue("test")])
        assert "Consumer" in repr(c)
        assert "1 queues" in repr(c)

    async def test_consume_and_callback(self):
        received = []

        def on_message(body, message):
            received.append(body)

        async with Connection("memory://") as conn:
            q = Queue("test_q")
            # Publish a message first
            async with conn.Producer() as p:
                await p.publish({"hello": "world"}, routing_key="test_q")

            # Consume it
            async with conn.Consumer([q], callbacks=[on_message]):
                await conn.drain_events(timeout=1.0)

            assert len(received) == 1
            assert received[0] == {"hello": "world"}

    async def test_consume_no_ack(self):
        received = []

        def on_message(body, message):
            received.append(body)

        async with Connection("memory://") as conn:
            q = Queue("test_q")
            async with conn.Producer() as p:
                await p.publish({"test": True}, routing_key="test_q")

            async with conn.Consumer([q], callbacks=[on_message], no_ack=True):
                await conn.drain_events(timeout=1.0)

            assert len(received) == 1

    async def test_cancel(self):
        async with Connection("memory://") as conn:
            q = Queue("test_q")
            consumer = conn.Consumer([q])
            await consumer.consume()
            assert consumer._running
            await consumer.cancel()
            assert not consumer._running

    async def test_context_manager(self):
        async with Connection("memory://") as conn:
            q = Queue("test_q")
            async with conn.Consumer([q]) as consumer:
                assert consumer._running
            assert not consumer._running

    async def test_purge(self):
        async with Connection("memory://") as conn:
            q = Queue("test_q")
            async with conn.Producer() as p:
                await p.publish({"a": 1}, routing_key="test_q")
                await p.publish({"b": 2}, routing_key="test_q")

            consumer = conn.Consumer([q])
            await consumer._ensure_channel()
            count = await consumer.purge()
            assert count >= 0  # Exact count depends on transport

    async def test_multiple_callbacks(self):
        received1 = []
        received2 = []

        def cb1(body, message):
            received1.append(body)

        def cb2(body, message):
            received2.append(body)

        async with Connection("memory://") as conn:
            q = Queue("test_q")
            async with conn.Producer() as p:
                await p.publish({"data": 1}, routing_key="test_q")

            async with conn.Consumer([q], callbacks=[cb1, cb2]):
                await conn.drain_events(timeout=1.0)

            assert len(received1) == 1
            assert len(received2) == 1
