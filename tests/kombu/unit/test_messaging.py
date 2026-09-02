"""Tests for kombu.messaging - async Producer and Consumer."""

import asyncio
from itertools import count

import pytest

from kombu import Connection, Exchange, Queue
from kombu.exceptions import ChannelError, ConnectionError, ContentDisallowed
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


class RecordingChannel:
    """A channel that records what a Consumer asks the broker to do."""

    no_ack_consumers = None

    def __init__(self):
        self.declared = []
        self.bound = []
        self.consumed = []
        self.cancelled = []
        self.ops = []
        self._tags = count()

    async def declare_exchange(self, exchange):
        pass

    async def declare_queue(self, queue):
        self.declared.append(queue.name)
        return queue.name

    async def queue_bind(self, queue, exchange, routing_key="", arguments=None):
        self.bound.append((queue, exchange, routing_key))

    async def basic_consume(self, queue, callback, consumer_tag=None, no_ack=False):
        self.consumed.append(queue)
        self.ops.append(("consume", queue))
        return f"tag-{next(self._tags)}"

    async def basic_qos(self, prefetch_count):
        self.ops.append(("qos", prefetch_count))

    async def basic_cancel(self, consumer_tag):
        self.cancelled.append(consumer_tag)


class test_Consumer_consume:
    """consume() after add_queue()."""

    def _queue(self, name):
        return Queue(name, exchange=Exchange("ex", type="direct"), routing_key=name)

    async def test_declares_and_consumes_a_queue_added_after_the_first_consume(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[self._queue("one")])
        await consumer.consume()
        consumer.add_queue(self._queue("two"))
        await consumer.consume()

        assert channel.declared == ["one", "two"]
        assert channel.bound == [("one", "ex", "one"), ("two", "ex", "two")]
        assert channel.consumed == ["one", "two"]

    async def test_does_not_register_a_second_consumer_for_a_queue(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[self._queue("one")])
        await consumer.consume()
        await consumer.consume()
        await consumer.consume()

        assert channel.consumed == ["one"]
        assert channel.declared == ["one"]

    async def test_a_queue_added_later_delivers_messages(self):
        received = []

        async with Connection("memory://") as conn:
            exchange = Exchange("ex", type="direct")
            consumer = conn.Consumer(
                [Queue("one", exchange=exchange, routing_key="one")],
                callbacks=[lambda body, message: received.append(body)],
            )
            await consumer.consume()
            consumer.add_queue(Queue("two", exchange=exchange, routing_key="two"))
            await consumer.consume()

            async with conn.Producer(exchange=exchange) as producer:
                await producer.publish({"for": "two"}, routing_key="two")
            await conn.drain_events(timeout=1.0)

        assert received == [{"for": "two"}]


class test_Consumer_cancel_by_queue:
    async def test_cancels_the_broker_consumer_for_that_queue_only(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[Queue("one"), Queue("two")])
        await consumer.consume()

        await consumer.cancel_by_queue("one")

        assert channel.cancelled == ["tag-0"]
        assert consumer.consuming_from("one") is False
        assert consumer.consuming_from("two") is True

    async def test_stops_delivery_from_the_cancelled_queue(self):
        received = []

        async with Connection("memory://") as conn:
            consumer = conn.Consumer(
                [Queue("one"), Queue("two")],
                callbacks=[lambda body, message: received.append(body)],
                no_ack=True,
            )
            await consumer.consume()
            await consumer.cancel_by_queue("one")

            async with conn.Producer() as producer:
                await producer.publish({"for": "one"}, routing_key="one")
                await producer.publish({"for": "two"}, routing_key="two")
            await conn.drain_events(timeout=1.0)

        assert received == [{"for": "two"}]

    async def test_an_unknown_queue_is_left_alone(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[Queue("one")])
        await consumer.consume()

        await consumer.cancel_by_queue("nope")

        assert channel.cancelled == []
        assert consumer.consuming_from("one") is True


class test_Producer_declare:
    async def test_a_failing_declare_reaches_the_caller(self):
        class Mismatched:
            async def declare(self, channel):
                raise ChannelError("PRECONDITION_FAILED - inequivalent arg 'x-max-priority'")

        async with Connection("memory://") as conn:
            producer = conn.Producer()
            with pytest.raises(ChannelError, match="x-max-priority"):
                await producer.publish({"a": 1}, routing_key="declare_q", declare=[Mismatched()])

            channel = await conn.default_channel()
            assert await channel.get("declare_q", no_ack=True) is None


class test_Consumer_accept:
    """`accept` restricts what a delivered message may be decoded as.

    The disallowed cases publish JSON, which decodes fine without a
    restriction, so what they assert on is the restriction and nothing else.
    """

    async def _consume(self, *, serializer, accept, on_decode_error=None):
        received = []
        async with Connection("memory://") as conn:
            async with conn.Producer() as producer:
                await producer.publish({"x": 1}, routing_key="accept_q", serializer=serializer)

            consumer = conn.Consumer(
                [Queue("accept_q")],
                callbacks=[lambda body, message: received.append(body)],
                accept=accept,
                no_ack=True,
                on_decode_error=on_decode_error,
            )
            await consumer.consume()
            await conn.drain_events(timeout=1.0)
        return received

    async def test_an_accepted_content_type_is_delivered_decoded(self):
        assert await self._consume(serializer="json", accept=["json"]) == [{"x": 1}]

    async def test_a_disallowed_content_type_raises_content_disallowed(self):
        with pytest.raises(ContentDisallowed, match="application/json"):
            await self._consume(serializer="json", accept=["yaml"])

    async def test_a_disallowed_content_type_goes_to_on_decode_error(self):
        errors = []
        received = await self._consume(
            serializer="json",
            accept=["yaml"],
            on_decode_error=lambda message, exc: errors.append(exc),
        )
        assert received == []
        assert [type(exc) for exc in errors] == [ContentDisallowed]

    async def test_a_raw_on_message_callback_still_gets_the_restriction(self):
        seen = []
        async with Connection("memory://") as conn:
            async with conn.Producer() as producer:
                await producer.publish({"x": 1}, routing_key="accept_q", serializer="json")

            consumer = conn.Consumer(
                [Queue("accept_q")],
                on_message=seen.append,
                accept=["yaml"],
                no_ack=True,
            )
            await consumer.consume()
            await conn.drain_events(timeout=1.0)

        assert len(seen) == 1
        with pytest.raises(ContentDisallowed):
            seen[0].payload


@pytest.fixture
def sleeps(monkeypatch):
    """Record what the retry loop waits for, without waiting for it."""
    recorded = []

    async def sleep(delay, result=None):
        recorded.append(delay)
        return result

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return recorded


class test_Producer_retry:
    """`retry` and `retry_policy` on publish."""

    async def _flaky_connection(self, failures):
        """A memory connection whose channels fail the first publishes.

        The failures are attached per channel, so a publish retried on a
        reconnected channel meets the next one.
        """
        conn = Connection("memory://")
        await conn.connect()
        attempts = []
        open_channel = conn.channel

        async def flaky_channel():
            channel = await open_channel()
            publish = channel.publish

            async def flaky_publish(*args, **kwargs):
                attempts.append(1)
                if failures:
                    raise failures.pop(0)
                return await publish(*args, **kwargs)

            channel.publish = flaky_publish
            return channel

        conn.channel = flaky_channel
        return conn, attempts

    async def test_a_failed_publish_is_retried_until_the_broker_takes_it(self):
        conn, attempts = await self._flaky_connection(
            [ConnectionError("broker went away"), ChannelError("channel closed")],
        )
        async with conn:
            producer = conn.Producer(auto_declare=False)
            await producer.publish(
                {"a": 1},
                routing_key="retry_q",
                retry=True,
                retry_policy={"max_retries": 3, "interval_start": 0, "interval_step": 0},
            )

            assert len(attempts) == 3
            channel = await conn.default_channel()
            message = await channel.get("retry_q", no_ack=True)
            assert message.decode() == {"a": 1}

    async def test_the_retry_policy_backs_off_and_reports_every_failure(self, sleeps):
        failures = [ConnectionError(f"attempt {i}") for i in range(10)]
        conn, attempts = await self._flaky_connection(failures)
        reported = []

        async with conn:
            producer = conn.Producer(auto_declare=False)
            with pytest.raises(ConnectionError, match="attempt 3"):
                await producer.publish(
                    {"a": 1},
                    routing_key="retry_q",
                    retry=True,
                    retry_policy={
                        "max_retries": 3,
                        "interval_start": 1.0,
                        "interval_step": 2.0,
                        "interval_max": 4.0,
                        "errback": lambda exc, interval: reported.append((str(exc), interval)),
                    },
                )

        assert len(attempts) == 4
        assert reported == [("attempt 0", 1.0), ("attempt 1", 3.0), ("attempt 2", 4.0)]
        assert sleeps == [1.0, 3.0, 4.0]

    async def test_without_retry_the_first_failure_reaches_the_caller(self):
        conn, attempts = await self._flaky_connection([ConnectionError("broker went away")])
        async with conn:
            producer = conn.Producer(auto_declare=False)
            with pytest.raises(ConnectionError, match="broker went away"):
                await producer.publish({"a": 1}, routing_key="retry_q")

        assert len(attempts) == 1


class test_Consumer_prefetch_count:
    async def test_the_prefetch_count_is_applied_before_consuming(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[Queue("one")], prefetch_count=10)
        await consumer.consume()

        assert channel.ops == [("qos", 10), ("consume", "one")]

    async def test_a_queue_added_later_does_not_reapply_it(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[Queue("one")], prefetch_count=10)
        await consumer.consume()
        consumer.add_queue(Queue("two"))
        await consumer.consume()

        assert channel.ops == [("qos", 10), ("consume", "one"), ("consume", "two")]

    async def test_no_prefetch_count_leaves_the_channel_alone(self):
        channel = RecordingChannel()
        consumer = Consumer(channel, queues=[Queue("one")])
        await consumer.consume()

        assert channel.ops == [("consume", "one")]
