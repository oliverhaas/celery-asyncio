"""Integration tests for pure asyncio Redis transport."""

import asyncio

import pytest

from kombu import Connection, Exchange, Queue

pytestmark = pytest.mark.asyncio(loop_scope="function")

REDIS_URL = "redis://localhost:6379"


@pytest.fixture
async def connection():
    """Create a connection fixture."""
    conn = Connection(REDIS_URL)
    await conn.connect()
    yield conn
    await conn.close()


@pytest.fixture
async def channel(connection):
    """Create a channel fixture."""
    return await connection.channel()


class TestConnection:
    """Test Connection class."""

    async def test_connect(self):
        """Test basic connection."""
        async with Connection(REDIS_URL) as conn:
            assert conn.is_connected
            assert conn.transport is not None

    async def test_connect_and_close(self):
        """Test connect and close."""
        conn = Connection(REDIS_URL)
        await conn.connect()
        assert conn.is_connected
        await conn.close()
        assert not conn.is_connected

    async def test_channel(self, connection):
        """Test creating a channel."""
        channel = await connection.channel()
        assert channel is not None
        assert channel.client is not None


class TestChannel:
    """Test Channel class."""

    async def test_publish_and_get(self, channel):
        """Test publish and get message."""
        queue_name = "test_publish_and_get"

        # Clean up first
        await channel.queue_purge(queue_name)

        # Publish a message (body must be valid JSON since content-type is application/json)
        message = (
            b'{"body": {"key": "hello"}, "content-type": "application/json", '
            b'"content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )
        await channel.publish(message, exchange="", routing_key=queue_name)

        # Get the message
        msg = await channel.get(queue_name, no_ack=True)
        assert msg is not None
        assert msg.payload == {"key": "hello"}

        # Clean up
        await channel.queue_purge(queue_name)

    async def test_queue_purge(self, channel):
        """Test queue purge."""
        queue_name = "test_queue_purge"

        # Publish some messages
        message = (
            b'{"body": {"v": "test"}, "content-type": "application/json", '
            b'"content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )
        await channel.publish(message, exchange="", routing_key=queue_name)
        await channel.publish(message, exchange="", routing_key=queue_name)
        await channel.publish(message, exchange="", routing_key=queue_name)

        # Purge
        count = await channel.queue_purge(queue_name)
        assert count == 3

    async def test_ack_message(self, channel):
        """Test message acknowledgment."""
        queue_name = "test_ack_message"
        await channel.queue_purge(queue_name)

        # Publish
        message = (
            b'{"body": {"v": "ack_test"}, "content-type": "application/json", '
            b'"content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )
        await channel.publish(message, exchange="", routing_key=queue_name)

        # Get without auto-ack
        msg = await channel.get(queue_name, no_ack=False)
        assert msg is not None

        # Should be in delivered (unacked tracking)
        assert msg.delivery_tag in channel._delivered

        # Ack it
        await msg.ack()

        # Should no longer be in delivered
        assert msg.delivery_tag not in channel._delivered
        assert msg.acknowledged

        await channel.queue_purge(queue_name)

    async def test_reject_message(self, channel):
        """Test message rejection with requeue."""
        queue_name = "test_reject_message"
        await channel.queue_purge(queue_name)

        # Publish
        message = (
            b'{"body": {"v": "reject_test"}, "content-type": "application/json", '
            b'"content-encoding": "utf-8", "properties": {}, "headers": {}}'
        )
        await channel.publish(message, exchange="", routing_key=queue_name)

        # Get without auto-ack
        msg = await channel.get(queue_name, no_ack=False)
        assert msg is not None

        # Reject with requeue
        await msg.reject(requeue=True)

        # Message should be back in queue
        msg2 = await channel.get(queue_name, no_ack=True)
        assert msg2 is not None

        await channel.queue_purge(queue_name)


class TestProducer:
    """Test Producer class."""

    async def test_publish(self, connection):
        """Test Producer publish."""
        queue_name = "test_producer_publish"
        channel = await connection.channel()
        await channel.queue_purge(queue_name)

        async with connection.Producer() as producer:
            await producer.publish(
                {"hello": "world"},
                routing_key=queue_name,
            )

        # Verify message
        msg = await channel.get(queue_name, no_ack=True)
        assert msg is not None
        assert msg.payload == {"hello": "world"}

        await channel.queue_purge(queue_name)

    async def test_publish_with_serializer(self, connection):
        """Test Producer with different serializer."""
        queue_name = "test_producer_serializer"
        channel = await connection.channel()
        await channel.queue_purge(queue_name)

        async with connection.Producer(serializer="json") as producer:
            await producer.publish(
                {"key": "value", "number": 42},
                routing_key=queue_name,
            )

        msg = await channel.get(queue_name, no_ack=True)
        assert msg is not None
        assert msg.payload["key"] == "value"
        assert msg.payload["number"] == 42

        await channel.queue_purge(queue_name)


class TestConsumer:
    """Test Consumer class."""

    async def test_consume(self, connection):
        """Test Consumer consume."""
        queue_name = "test_consumer_consume"
        channel = await connection.channel()
        await channel.queue_purge(queue_name)

        received = []

        def callback(body, message):
            received.append(body)

        queue = Queue(queue_name)

        # Publish first
        async with connection.Producer() as producer:
            await producer.publish({"test": "message"}, routing_key=queue_name)

        # Consume
        async with connection.Consumer([queue], callbacks=[callback]):
            # One iteration should deliver the message
            try:
                await asyncio.wait_for(
                    connection.drain_events(timeout=2),
                    timeout=5,
                )
            except Exception:
                pass  # Timeout is expected

        assert len(received) == 1
        assert received[0] == {"test": "message"}

        await channel.queue_purge(queue_name)


class TestSimpleQueue:
    """Test SimpleQueue class."""

    async def test_put_and_get(self, connection):
        """Test SimpleQueue put and get."""
        async with connection.SimpleQueue("test_simple_queue") as queue:
            await queue.put({"hello": "simple"})

            msg = await queue.get(timeout=5)
            assert msg is not None
            assert msg.payload == {"hello": "simple"}
            await msg.ack()

            await queue.clear()

    async def test_get_nowait_empty(self, connection):
        """Test get_nowait on empty queue."""
        async with connection.SimpleQueue("test_simple_empty") as queue:
            await queue.clear()

            with pytest.raises(queue.Empty):
                await queue.get_nowait()

    async def test_multiple_messages(self, connection):
        """Test multiple messages through SimpleQueue."""
        async with connection.SimpleQueue("test_simple_multi") as queue:
            await queue.clear()

            # Put multiple messages
            for i in range(5):
                await queue.put({"index": i})

            # Get all messages (order within same-score is lexicographic by
            # delivery tag — with UUID tags this is not strictly FIFO)
            received = set()
            for _ in range(5):
                msg = await queue.get(timeout=5)
                received.add(msg.payload["index"])
                await msg.ack()

            assert received == {0, 1, 2, 3, 4}

            await queue.clear()


class TestExchangeTypes:
    """Test exchange type routing."""

    async def test_direct_exchange(self, connection):
        """Test direct exchange routing."""
        queue_name = "test_direct_exchange_queue"
        exchange_name = "test_direct_exchange"

        channel = await connection.channel()
        await channel.queue_purge(queue_name)

        # Declare exchange and queue
        exchange = Exchange(exchange_name, type="direct")
        await channel.declare_exchange(exchange)

        # Bind queue to exchange
        await channel.queue_bind(
            queue=queue_name,
            exchange=exchange_name,
            routing_key="test.key",
        )

        # Publish to exchange
        async with connection.Producer(exchange=exchange) as producer:
            await producer.publish(
                {"data": "direct"},
                routing_key="test.key",
            )

        # Should receive message
        msg = await channel.get(queue_name, no_ack=True)
        assert msg is not None
        assert msg.payload["data"] == "direct"

        await channel.queue_purge(queue_name)

    async def test_topic_exchange_pattern_matching(self, channel):
        """Test topic exchange pattern matching."""
        from kombu.transport.valkey_redis import _topic_match

        assert _topic_match("user.created", "user.*") is True
        assert _topic_match("user.created", "user.#") is True
        assert _topic_match("user.profile.updated", "user.#") is True
        assert _topic_match("user.created", "order.*") is False
        assert _topic_match("user.profile.updated", "user.*") is False


JSON_MESSAGE = (
    b'{"body": {"v": "x"}, "content-type": "application/json", '
    b'"content-encoding": "utf-8", "properties": {}, "headers": {}}'
)


async def run_sweep(channel, queue):
    """Run one enqueue_due_messages pass over ``queue``.

    The sweep only visits queues that have a consumer, so register one. Its
    callback is never invoked: the sweep moves tags between keys and does not
    deliver.
    """
    channel._consumers["sweep-probe"] = (queue, lambda *args: None, False)
    try:
        return await channel._enqueue_due_messages()
    finally:
        del channel._consumers["sweep-probe"]


async def expire_visibility(channel, queue, delivery_tag):
    """Backdate a delivery's visibility deadline so the next sweep restores it."""
    await channel.client.zadd(channel._messages_index_key(queue), {delivery_tag: 0})


class TestDeliveryTracking:
    """Regressions for the fixes ported from celery-redis-plus (see PORT-PLAN.md)."""

    async def test_acking_after_a_restore_cancels_the_restored_copy(self, channel):
        """PORT-PLAN fix 1."""
        queue_name = "test_ack_cancels_restore"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        msg = await channel.get(queue_name, no_ack=False)
        assert msg is not None
        assert await channel.client.zcard(channel._queue_key(queue_name)) == 0

        # The consumer is still working on it when the deadline passes, so the
        # sweep puts a second poppable copy back in the queue.
        await expire_visibility(channel, queue_name, msg.delivery_tag)
        enqueued, _dropped = await run_sweep(channel, queue_name)
        assert enqueued == 1
        assert await channel.client.zcard(channel._queue_key(queue_name)) == 1

        # The ack has to cancel that copy, or a second worker runs the task.
        await msg.ack()
        assert await channel.client.zcard(channel._queue_key(queue_name)) == 0

        await channel.queue_purge(queue_name)

    async def test_rejecting_without_requeue_cancels_the_restored_copy(self, channel):
        """PORT-PLAN fix 1, the other caller of the ack script."""
        queue_name = "test_reject_cancels_restore"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        msg = await channel.get(queue_name, no_ack=False)
        assert msg is not None

        await expire_visibility(channel, queue_name, msg.delivery_tag)
        await run_sweep(channel, queue_name)
        assert await channel.client.zcard(channel._queue_key(queue_name)) == 1

        await msg.reject(requeue=False)
        assert await channel.client.zcard(channel._queue_key(queue_name)) == 0

        await channel.queue_purge(queue_name)

    async def test_consuming_recreates_a_missing_index_entry(self, channel):
        """PORT-PLAN fix 2."""
        queue_name = "test_consume_recreates_index"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        # Drop the index entry. A delivery that leaves nothing tracking the
        # message is out of the queue and out of the index, so a worker crash
        # loses it permanently.
        index_key = channel._messages_index_key(queue_name)
        await channel.client.delete(index_key)

        msg = await channel.get(queue_name, no_ack=False)
        assert msg is not None
        assert await channel.client.zscore(index_key, msg.delivery_tag) is not None

        await msg.ack()
        await channel.queue_purge(queue_name)

    async def test_a_backlogged_message_is_not_counted_as_redelivered(self, channel):
        """PORT-PLAN fix 3."""
        queue_name = "test_backlog_not_redelivered"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        index_key = channel._messages_index_key(queue_name)
        [tag_raw] = await channel.client.zrange(index_key, 0, -1)
        tag = tag_raw.decode() if isinstance(tag_raw, bytes) else tag_raw

        # Nobody consumed it. It is still in the queue, waiting behind a
        # backlog, and its deadline passes. That is not a redelivery.
        await expire_visibility(channel, queue_name, tag)
        enqueued, _dropped = await run_sweep(channel, queue_name)

        assert enqueued == 0
        counter = await channel.client.hget(channel._message_key(tag), "delivery_count")
        assert int(counter or 0) == 0

        # The deadline still moves forward, or every cycle re-checks the tag.
        assert await channel.client.zscore(index_key, tag) > 0

        await channel.queue_purge(queue_name)

    async def test_a_no_ack_delivery_leaves_nothing_behind(self, channel):
        """PORT-PLAN fix 4."""
        queue_name = "test_no_ack_dequeues"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        msg = await channel.get(queue_name, no_ack=True)
        assert msg is not None

        # Nothing will ever ack a no_ack delivery, so an index entry left here
        # leaks and the next sweep redelivers the message.
        assert await channel.client.zcard(channel._messages_index_key(queue_name)) == 0
        assert await channel.client.exists(channel._message_key(msg.delivery_tag)) == 0

        await channel.queue_purge(queue_name)

    async def test_a_first_delivery_is_not_flagged_as_redelivered(self, channel):
        """PORT-PLAN fix 5."""
        queue_name = "test_first_delivery_not_redelivered"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        msg = await channel.get(queue_name, no_ack=False)
        assert msg.delivery_info["redelivered"] is False
        assert "x-delivery-count" not in msg.headers

        await msg.ack()
        await channel.queue_purge(queue_name)

    async def test_a_restored_message_is_flagged_as_redelivered(self, channel):
        """PORT-PLAN fix 5.

        celery gates ``worker_deduplicate_successful_tasks`` on
        ``delivery_info['redelivered']``, so the counter has to surface there.
        """
        queue_name = "test_restore_sets_redelivered"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        first = await channel.get(queue_name, no_ack=False)
        await expire_visibility(channel, queue_name, first.delivery_tag)
        await run_sweep(channel, queue_name)

        second = await channel.get(queue_name, no_ack=False)
        assert second.delivery_info["redelivered"] is True
        assert second.headers["x-delivery-count"] == 1

        await second.ack()
        await channel.queue_purge(queue_name)

    async def test_rejecting_with_requeue_counts_as_a_redelivery(self, channel):
        """PORT-PLAN fix 5.

        A reject-with-requeue is a redelivery in AMQP, the same as a visibility
        timeout restore, so it moves the same counter.
        """
        queue_name = "test_requeue_counts"
        await channel.queue_purge(queue_name)
        await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

        first = await channel.get(queue_name, no_ack=False)
        await first.reject(requeue=True)

        second = await channel.get(queue_name, no_ack=False)
        assert second.delivery_info["redelivered"] is True
        assert second.headers["x-delivery-count"] == 1

        await second.ack()
        await channel.queue_purge(queue_name)

    async def test_the_delivery_limit_counts_attempts(self):
        """PORT-PLAN fix 6.

        RabbitMQ's delivery-limit is the number of attempts allowed, so a limit
        of 1 means the message is delivered once and then dropped.
        """
        queue_name = "test_delivery_limit_counts_attempts"
        async with Connection(REDIS_URL, transport_options={"delivery_limit": 1}) as conn:
            channel = await conn.channel()
            await channel.queue_purge(queue_name)
            await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

            msg = await channel.get(queue_name, no_ack=False)
            await expire_visibility(channel, queue_name, msg.delivery_tag)
            enqueued, dropped = await run_sweep(channel, queue_name)

            assert (enqueued, dropped) == (0, 1)
            assert await channel.client.exists(channel._message_key(msg.delivery_tag)) == 0
            assert await channel.client.zcard(channel._queue_key(queue_name)) == 0

    async def test_a_reject_loop_stops_at_the_delivery_limit(self):
        """PORT-PLAN fix 6.

        The sweep cannot catch a live reject loop, because every consume
        re-stamps the index deadline and the entry never comes due there. So the
        requeue script enforces the limit itself.
        """
        queue_name = "test_reject_loop_stops"
        async with Connection(REDIS_URL, transport_options={"delivery_limit": 2}) as conn:
            channel = await conn.channel()
            await channel.queue_purge(queue_name)
            await channel.publish(JSON_MESSAGE, exchange="", routing_key=queue_name)

            first = await channel.get(queue_name, no_ack=False)
            await first.reject(requeue=True)

            second = await channel.get(queue_name, no_ack=False)
            assert second.headers["x-delivery-count"] == 1
            await second.reject(requeue=True)

            # A third attempt would exceed a limit of 2, so this requeue drops.
            assert await channel.client.zcard(channel._queue_key(queue_name)) == 0
            assert await channel.client.exists(channel._message_key(second.delivery_tag)) == 0
            assert await channel.get(queue_name, no_ack=False) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
