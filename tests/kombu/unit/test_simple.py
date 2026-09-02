"""Tests for kombu.simple - SimpleQueue and SimpleBuffer."""

import pytest

from kombu import Connection
from kombu.exceptions import ContentDisallowed
from kombu.simple import SimpleBuffer


class test_SimpleQueue:
    """Tests for SimpleQueue class."""

    async def test_put_get(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("test_sq") as sq:
            await sq.put({"hello": "world"})
            msg = await sq.get(timeout=1)
            assert msg.payload == {"hello": "world"}
            await msg.ack()

    async def test_put_get_multiple(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("test_sq") as sq:
            for i in range(5):
                await sq.put({"num": i})

            for i in range(5):
                msg = await sq.get(timeout=1)
                assert msg.payload == {"num": i}
                await msg.ack()

    async def test_get_empty(self):
        from queue import Empty

        async with Connection("memory://") as conn, conn.SimpleQueue("test_sq") as sq:
            with pytest.raises(Empty):
                await sq.get(timeout=0.01)

    async def test_context_manager(self):
        conn = Connection("memory://")
        await conn.connect()
        sq = conn.SimpleQueue("test_sq")
        async with sq:
            await sq.put({"test": True})
        await conn.close()

    async def test_close(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("test_sq") as sq:
            await sq.put({"test": True})
            # After exiting context, consumer should be closed

    async def test_an_option_the_queue_does_not_know_is_rejected(self):
        # SimpleQueue ended its signature in **kwargs, so an option meant for
        # the queue or the exchange was accepted and then dropped.
        async with Connection("memory://") as conn:
            with pytest.raises(TypeError, match="durable"):
                conn.SimpleQueue("test_sq", durable=True)


class test_SimpleBuffer:
    """Tests for SimpleBuffer class (transient messages)."""

    async def test_put_get(self):
        async with Connection("memory://") as conn, SimpleBuffer(conn, "test_buf") as buf:
            await buf.put({"data": "value"})
            msg = await buf.get(timeout=1)
            assert msg.payload == {"data": "value"}
            await msg.ack()


async def test_simple_buffer_declares_an_exclusive_queue():
    # RabbitMQ 4.3.0 rejects transient non-exclusive queues, and a buffer is
    # by definition owned by the connection that created it.
    async with Connection("memory://") as conn, SimpleBuffer(conn, "test_buf_excl") as buf:
        assert buf._queue.exclusive is True
        assert buf._queue.durable is False


class test_SimpleQueue_accept:
    """`accept` restricts what a message taken off the queue may decode as.

    The messages here are JSON, which decodes fine without a restriction, so
    what the tests assert on is the restriction and nothing else.
    """

    async def test_a_blocking_get_applies_the_restriction(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("accept_sq", accept=["yaml"]) as sq:
            await sq.put({"x": 1})
            message = await sq.get(timeout=1)
            with pytest.raises(ContentDisallowed, match="application/json"):
                message.payload

    async def test_a_non_blocking_get_applies_the_restriction(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("accept_sq", accept=["yaml"]) as sq:
            await sq.put({"x": 1})
            message = await sq.get(block=False)
            with pytest.raises(ContentDisallowed, match="application/json"):
                message.payload

    async def test_an_accepted_content_type_decodes(self):
        async with Connection("memory://") as conn, conn.SimpleQueue("accept_sq", accept=["json"]) as sq:
            await sq.put({"x": 1})
            message = await sq.get(timeout=1)
            assert message.payload == {"x": 1}
