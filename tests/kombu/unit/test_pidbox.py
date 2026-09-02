"""Tests for kombu.pidbox - Mailbox and Node."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from kombu import Connection
from kombu.exceptions import InconsistencyError
from kombu.pidbox import Mailbox


class Locked(Exception):
    """Stand-in for the driver's RESOURCE_LOCKED (405) exception."""


def _node(consume_raises=None, locked_errors=(Locked,)):
    """A Node whose Consumer is a stub, so listen() does no I/O."""
    mailbox = Mailbox("testns")
    mailbox.connection = Mock()
    mailbox.connection.resource_locked_errors = locked_errors
    node = mailbox.Node("test.node")
    consumer = Mock()
    consumer.consume = AsyncMock(side_effect=consume_raises)
    node.Consumer = Mock(return_value=consumer)
    return node


def test_mailbox_queues_are_exclusive_by_default():
    # RabbitMQ 4.3.0 refuses transient non-exclusive queues.
    mailbox = Mailbox("testns")
    assert mailbox.queue_exclusive is True
    assert mailbox.get_queue("test.node").exclusive is True
    assert mailbox.get_reply_queue().exclusive is True


def test_asking_only_for_a_durable_mailbox_still_works():
    # The default must not turn into a ValueError for a caller that only
    # asked for durability.
    mailbox = Mailbox("testns", queue_durable=True)
    assert mailbox.queue_exclusive is False
    assert mailbox.get_queue("test.node").durable is True


def test_explicitly_asking_for_both_exclusive_and_durable_is_rejected():
    with pytest.raises(ValueError, match="cannot both be True"):
        Mailbox("testns", queue_durable=True, queue_exclusive=True)


def test_explicitly_asking_for_a_shared_mailbox_is_honoured():
    assert Mailbox("testns", queue_exclusive=False).queue_exclusive is False


async def test_listen_reports_a_locked_pidbox_queue_by_hostname():
    node = _node(Locked("RESOURCE_LOCKED"))

    with pytest.raises(InconsistencyError, match=r"test\.node") as excinfo:
        await node.listen()

    assert isinstance(excinfo.value.__cause__, Locked)


async def test_listen_does_not_swallow_other_channel_errors():
    node = _node(RuntimeError("something else"))

    with pytest.raises(RuntimeError, match="something else"):
        await node.listen()


async def test_listen_on_a_transport_without_exclusive_queues_passes_errors_through():
    node = _node(Locked("RESOURCE_LOCKED"), locked_errors=())

    with pytest.raises(Locked):
        await node.listen()


async def test_collect_stops_at_the_timeout_even_while_events_keep_arriving():
    # With no reply limit, a busy channel kept the loop draining forever.
    mailbox = Mailbox("testns")
    mailbox.connection = Mock()
    channel = Mock()
    mailbox.connection.default_channel = AsyncMock(return_value=channel)
    drained = 0

    async def drain_events(timeout=None):
        nonlocal drained
        drained += 1
        await asyncio.sleep(0)
        return True

    channel.drain_events = drain_events

    with patch("kombu.pidbox.Consumer", return_value=MagicMock()):
        started = time.monotonic()
        responses = await mailbox._collect("ticket", timeout=0.2)
        elapsed = time.monotonic() - started

    assert responses == []
    assert drained > 1
    assert elapsed < 5


async def _listen(mailbox, hostname, channel, seen):
    node = mailbox.Node(
        hostname,
        state=hostname,
        channel=channel,
        handlers={"ping": seen.append},
    )
    await node.listen(channel=channel)
    return node


async def _broadcast_and_drain(conn, mailbox, **kwargs):
    await mailbox._broadcast("ping", {}, reply=False, **kwargs)
    for _ in range(4):
        try:
            await conn.drain_events(timeout=0.05)
        except TimeoutError:
            break


async def test_broadcast_pattern_only_dispatches_on_matching_nodes():
    seen = []
    async with Connection("memory://") as conn:
        mailbox = Mailbox("testns", type="fanout")(conn)
        channel = await conn.default_channel()
        await _listen(mailbox, "worker-a1", channel, seen)
        await _listen(mailbox, "worker-b1", channel, seen)

        await _broadcast_and_drain(conn, mailbox, pattern="worker-a*", matcher="glob")

    assert seen == ["worker-a1"]


async def test_broadcast_without_a_pattern_dispatches_everywhere():
    seen = []
    async with Connection("memory://") as conn:
        mailbox = Mailbox("testns", type="fanout")(conn)
        channel = await conn.default_channel()
        await _listen(mailbox, "worker-a1", channel, seen)
        await _listen(mailbox, "worker-b1", channel, seen)

        await _broadcast_and_drain(conn, mailbox)

    assert sorted(seen) == ["worker-a1", "worker-b1"]


async def _reply_content_type(serializer):
    async with Connection("memory://") as conn:
        mailbox = Mailbox("testns", type="fanout", serializer=serializer)(conn)
        channel = await conn.default_channel()
        node = mailbox.Node("worker-a1", channel=channel)
        await node.reply({"worker-a1": "pong"}, "reply.testns.pidbox", "replies", "ticket")
        message = await channel.get("replies", no_ack=True)
    return message.content_type


async def test_reply_uses_the_mailbox_serializer():
    assert await _reply_content_type("pickle") == "application/x-python-serialize"


async def test_reply_falls_back_to_json():
    assert await _reply_content_type(None) == "application/json"


def _dispatch_node(handlers):
    return Mailbox("testns").Node("test.node", state="worker-a1", handlers=handlers)


async def test_dispatch_awaits_an_async_handler():
    async def ping(state):
        await asyncio.sleep(0)
        return f"pong from {state}"

    assert await _dispatch_node({"ping": ping}).dispatch("ping") == "pong from worker-a1"


async def test_dispatch_reports_an_async_handler_that_raises():
    async def boom(state):
        raise KeyError("no such command")

    assert await _dispatch_node({"boom": boom}).dispatch("boom") == {"error": "KeyError('no such command')"}


async def test_collect_consumes_on_the_channel_it_was_given():
    async with Connection("memory://") as conn:
        channel = await conn.channel()
        consumed = []
        basic_consume = channel.basic_consume

        async def recording_basic_consume(queue, *args, **kwargs):
            consumed.append(queue)
            return await basic_consume(queue, *args, **kwargs)

        channel.basic_consume = recording_basic_consume
        mailbox = Mailbox("testns")(conn)

        responses = await mailbox._collect("ticket", timeout=0.05, channel=channel)

    assert responses == []
    assert consumed == [mailbox.reply_queue.name]


async def test_a_reply_arrives_on_a_caller_supplied_channel():
    # Collecting through the connection drained its default channel, where
    # no reply arrives.
    async with Connection("memory://") as conn:
        channel = await conn.channel()
        mailbox = Mailbox("testns", type="fanout", accept=["json"])(conn)
        node = mailbox.Node(
            "worker-a1",
            state="worker-a1",
            channel=channel,
            handlers={"ping": lambda state: state},
        )
        await node.listen(channel=channel)

        replies = await mailbox.multi_call("ping", timeout=0.2, channel=channel)

    assert replies == [{"worker-a1": "worker-a1"}]


def test_oid_stays_the_same_on_another_thread():
    mailbox = Mailbox("testns")
    seen = []
    thread = threading.Thread(target=lambda: seen.append(mailbox.oid))
    thread.start()
    thread.join()

    assert seen == [mailbox.oid]
    assert mailbox.get_reply_queue().routing_key == mailbox.oid
