"""Tests for kombu.simple - SimpleQueue and SimpleBuffer."""

import pytest

from kombu import Connection
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
