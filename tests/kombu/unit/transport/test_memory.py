"""Tests for the in-memory transport."""

import asyncio
import logging
import threading
import time

import pytest

from kombu.entity import Exchange, Queue
from kombu.transport.memory import MAX_WAIT, Channel, Transport
from kombu.utils.json import dumps as json_dumps


def envelope(body: str, **kwargs) -> bytes:
    payload = {"body": body, "content-type": "text/plain", "content-encoding": "utf-8"}
    payload.update(kwargs)
    return json_dumps(payload).encode()


async def make_channel() -> Channel:
    return await Transport().create_channel()


class test_publish_and_get:
    async def test_default_exchange_routes_by_queue_name(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))

        await channel.publish(envelope("hi"), "", "q")

        message = await channel.get("q")
        assert message.body == b"hi"
        assert message.content_type == "text/plain"
        assert await channel.get("q") is None

    async def test_direct_exchange_routes_by_routing_key(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("direct.ex", type="direct"))
        await channel.queue_bind("wanted", "direct.ex", "rk")

        await channel.publish(envelope("hi"), "direct.ex", "rk")
        await channel.publish(envelope("no"), "direct.ex", "other")

        assert (await channel.get("wanted")).body == b"hi"
        assert await channel.get("wanted") is None

    async def test_fanout_exchange_copies_to_every_bound_queue(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("fan.ex", type="fanout"))
        await channel.queue_bind("one", "fan.ex")
        await channel.queue_bind("two", "fan.ex")

        await channel.publish(envelope("hi"), "fan.ex", "ignored")

        assert (await channel.get("one")).body == b"hi"
        assert (await channel.get("two")).body == b"hi"

    async def test_topic_exchange_matches_patterns(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("topic.ex", type="topic"))
        await channel.queue_bind("star", "topic.ex", "a.*")
        await channel.queue_bind("hash", "topic.ex", "a.#")

        await channel.publish(envelope("one"), "topic.ex", "a.b")
        await channel.publish(envelope("two"), "topic.ex", "a.b.c")

        assert (await channel.get("star")).body == b"one"
        assert await channel.get("star") is None
        assert (await channel.get("hash")).body == b"one"
        assert (await channel.get("hash")).body == b"two"

    async def test_unbind_stops_delivery(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("fan.ex", type="fanout"))
        await channel.queue_bind("one", "fan.ex")
        await channel.queue_unbind("one", "fan.ex")

        await channel.publish(envelope("hi"), "fan.ex", "")

        assert await channel.get("one") is None

    async def test_payload_that_is_not_an_envelope_is_delivered_raw(self, caplog):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(b"[1, 2, 3]", "", "q")

        with caplog.at_level(logging.ERROR, logger="kombu.transport.base"):
            message = await channel.get("q")

        assert message.body == b"[1, 2, 3]"
        assert message.content_type == "application/data"
        assert "q" in caplog.text


class test_queue_management:
    async def test_purge_returns_the_number_dropped(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("one"), "", "q")
        await channel.publish(envelope("two"), "", "q")

        assert await channel.queue_purge("q") == 2
        assert await channel.get("q") is None
        assert await channel.queue_purge("q") == 0

    async def test_delete_removes_the_queue_and_its_bindings(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("fan.ex", type="fanout"))
        await channel.queue_bind("q", "fan.ex")
        await channel.publish(envelope("hi"), "fan.ex", "")

        assert await channel.queue_delete("q") == 1
        assert "q" not in Channel._queues
        assert Channel._bindings["fan.ex"] == []

    async def test_delete_if_empty_keeps_a_queue_with_messages(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("hi"), "", "q")

        assert await channel.queue_delete("q", if_empty=True) == 0
        assert (await channel.get("q")).body == b"hi"

    async def test_delete_of_an_unknown_queue(self):
        channel = await make_channel()

        assert await channel.queue_delete("nope") == 0


class test_acknowledgement:
    async def test_reject_with_requeue_puts_the_message_back(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await channel.basic_reject(message.delivery_tag, requeue=True)

        assert (await channel.get("q")).body == b"hi"

    async def test_reject_without_requeue_drops_the_message(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await channel.basic_reject(message.delivery_tag, requeue=False)

        assert await channel.get("q") is None

    async def test_recover_requeues_everything_outstanding(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("one"), "", "q")
        await channel.publish(envelope("two"), "", "q")
        await channel.get("q")
        await channel.get("q")

        await channel.basic_recover(requeue=True)

        assert (await channel.get("q")).body == b"one"
        assert (await channel.get("q")).body == b"two"

    async def test_ack_multiple_stops_at_the_tag(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        for body in ("one", "two", "three"):
            await channel.publish(envelope(body), "", "q")
        first = await channel.get("q")
        second = await channel.get("q")
        third = await channel.get("q")

        await channel.basic_ack(second.delivery_tag, multiple=True)

        assert first.delivery_tag not in channel._unacked
        assert second.delivery_tag not in channel._unacked
        assert third.delivery_tag in channel._unacked

    async def test_ack_multiple_of_an_unknown_tag_keeps_the_rest(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("one"), "", "q")
        message = await channel.get("q")
        await channel.basic_ack(message.delivery_tag)

        await channel.basic_ack(message.delivery_tag, multiple=True)

        await channel.publish(envelope("two"), "", "q")
        outstanding = await channel.get("q")
        await channel.basic_ack("bogus-tag", multiple=True)
        assert outstanding.delivery_tag in channel._unacked

    async def test_close_requeues_unacked_messages(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.publish(envelope("hi"), "", "q")
        await channel.get("q")

        await channel.close()

        assert list(Channel._queues["q"]) == [envelope("hi")]


class test_drain_events:
    async def test_delivers_a_queued_message(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("hi"), "", "q")

        assert await channel.drain_events(timeout=1) is True
        assert received == ["hi"]

    async def test_timeout_zero_polls_without_blocking(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.basic_consume("q", lambda body, message: None)

        started = time.monotonic()
        assert await channel.drain_events(timeout=0) is False
        assert time.monotonic() - started < 0.2

    async def test_timeout_zero_delivers_what_is_ready(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("hi"), "", "q")

        started = time.monotonic()
        assert await channel.drain_events(timeout=0) is True
        assert time.monotonic() - started < 0.2
        assert received == ["hi"]

    async def test_positive_timeout_is_honoured(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        await channel.basic_consume("q", lambda body, message: None)

        started = time.monotonic()
        assert await channel.drain_events(timeout=0.2) is False
        elapsed = time.monotonic() - started
        assert 0.2 <= elapsed < 0.6

    async def test_a_publish_wakes_a_waiting_drain(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))

        async def publish_soon():
            await asyncio.sleep(0.05)
            await channel.publish(envelope("hi"), "", "q")

        started = time.monotonic()
        publisher = asyncio.create_task(publish_soon())
        assert await channel.drain_events(timeout=MAX_WAIT * 3) is True
        elapsed = time.monotonic() - started
        await publisher

        assert received == ["hi"]
        # Woken by the publish, not by the next poll.
        assert elapsed < MAX_WAIT

    async def test_cancelling_a_drain_leaves_the_next_message_queued(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(channel.drain_events(timeout=5), 0.05)

        await channel.publish(envelope("hi"), "", "q")
        # Let anything the cancelled drain might have left running run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert await channel.drain_events(timeout=1) is True
        assert received == ["hi"]

    async def test_a_cancelled_consumer_is_not_served(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        received = []
        tag = await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.basic_cancel(tag)
        await channel.publish(envelope("hi"), "", "q")

        assert await channel.drain_events(timeout=0.1) is False
        assert received == []
        assert (await channel.get("q")).body == b"hi"

    async def test_cancelling_a_consumer_during_a_drain(self):
        channel = await make_channel()
        await channel.declare_queue(Queue("q"))
        tag = await channel.basic_consume("q", lambda body, message: None)

        async def cancel_soon():
            await asyncio.sleep(0)
            await channel.basic_cancel(tag)

        canceller = asyncio.create_task(cancel_soon())
        assert await channel.drain_events(timeout=0.1) is False
        await canceller

    async def test_a_busy_queue_does_not_starve_the_others(self):
        channel = await make_channel()
        received = []
        await channel.basic_consume("busy", lambda body, message: received.append(("busy", body)))
        await channel.basic_consume("quiet", lambda body, message: received.append(("quiet", body)))
        await channel.publish(envelope("reply"), "", "quiet")

        # The first queue is never empty, so a drain that always starts there
        # would never reach the second one.
        for _ in range(3):
            await channel.publish(envelope("task"), "", "busy")
            assert await channel.drain_events(timeout=1) is True

        assert ("quiet", "reply") in received

    async def test_a_body_that_cannot_be_deserialized_is_passed_on(self, caplog):
        channel = await make_channel()
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("{truncated", **{"content-type": "application/json"}), "", "q")

        with caplog.at_level(logging.WARNING, logger="kombu.transport.memory"):
            assert await channel.drain_events(timeout=1) is True

        assert received == [b"{truncated"]
        assert "Cannot decode message" in caplog.text

    async def test_no_consumers_still_honours_the_timeout(self):
        channel = await make_channel()

        started = time.monotonic()
        assert await channel.drain_events(timeout=0.2) is False
        assert time.monotonic() - started >= 0.2


class test_process_wide_state:
    """The queues outlive the connection and the event loop that made them."""

    def test_two_event_loops_in_sequence(self):
        async def publish():
            channel = await make_channel()
            await channel.declare_queue(Queue("q"))
            await channel.basic_consume("q", lambda body, message: None)
            # Block once so anything loop-bound would bind to this loop.
            await channel.drain_events(timeout=0.01)
            await channel.publish(envelope("hi"), "", "q")

        async def consume():
            channel = await make_channel()
            received = []
            await channel.basic_consume("q", lambda body, message: received.append(body))
            assert await channel.drain_events(timeout=1) is True
            return received

        asyncio.run(publish())
        assert asyncio.run(consume()) == ["hi"]

    def test_a_producer_and_a_consumer_on_separate_threads(self):
        received = []
        consuming = threading.Event()

        def consumer_thread():
            async def consume():
                channel = await make_channel()
                await channel.declare_queue(Queue("q"))
                await channel.basic_consume("q", lambda body, message: received.append(body))
                consuming.set()
                assert await channel.drain_events(timeout=5) is True

            asyncio.run(consume())

        async def publish():
            channel = await make_channel()
            await channel.publish(envelope("hi"), "", "q")

        thread = threading.Thread(target=consumer_thread)
        thread.start()
        assert consuming.wait(5)
        started = time.monotonic()
        asyncio.run(publish())
        thread.join(5)

        assert received == ["hi"]
        assert not thread.is_alive()
        # The drain woke on the publish rather than on its own poll interval.
        assert time.monotonic() - started < MAX_WAIT

    async def test_reset_state_clears_the_shared_queues(self):
        channel = await make_channel()
        await channel.declare_exchange(Exchange("fan.ex", type="fanout"))
        await channel.queue_bind("q", "fan.ex")
        await channel.publish(envelope("hi"), "fan.ex", "")

        Transport.reset_state()

        assert Channel._queues == {}
        assert Channel._exchanges == {}
        assert Channel._bindings == {}
        assert Channel._waiters == {}
