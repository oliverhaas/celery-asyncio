"""Tests for the filesystem transport."""

import asyncio
import logging
import time

import pytest

from kombu.entity import Exchange, Queue
from kombu.exceptions import ChannelError
from kombu.transport.filesystem import INFLIGHT_DIR, POLL_INTERVAL, Channel, Transport
from kombu.utils.json import dumps as json_dumps
from kombu.utils.json import loads as json_loads


def envelope(body: str, **kwargs) -> bytes:
    payload = {"body": body, "content-type": "text/plain", "content-encoding": "utf-8"}
    payload.update(kwargs)
    return json_dumps(payload).encode()


@pytest.fixture
def options(tmp_path):
    return {
        "data_folder_in": str(tmp_path / "data"),
        "data_folder_out": str(tmp_path / "data"),
        "control_folder": str(tmp_path / "control"),
        "processed_folder": str(tmp_path / "processed"),
    }


@pytest.fixture
def data_folder(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def control_folder(tmp_path):
    return tmp_path / "control"


async def make_channel(options, **overrides) -> Channel:
    return await Transport(**{**options, **overrides}).create_channel()


class test_publish_and_get:
    async def test_default_exchange_routes_by_queue_name(self, options):
        channel = await make_channel(options)
        await channel.declare_queue(Queue("q"))

        await channel.publish(envelope("hi"), "", "q")

        message = await channel.get("q")
        assert message.body == b"hi"
        assert message.content_type == "text/plain"
        assert await channel.get("q") is None

    async def test_messages_are_returned_oldest_first(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("one"), "", "q")
        await channel.publish(envelope("two"), "", "q")

        assert (await channel.get("q")).body == b"one"
        assert (await channel.get("q")).body == b"two"

    async def test_a_queue_does_not_take_another_queues_messages(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "b.a")

        assert await channel.get("a") is None
        assert (await channel.get("b.a")).body == b"hi"

    async def test_fanout_exchange_copies_to_every_bound_queue(self, options):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")
        await channel.queue_bind("two", "fan")

        await channel.publish(envelope("hi"), "fan", "ignored")

        assert (await channel.get("one")).body == b"hi"
        assert (await channel.get("two")).body == b"hi"

    async def test_topic_exchange_matches_patterns(self, options):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("topic", type="topic"))
        await channel.queue_bind("star", "topic", "a.*")

        await channel.publish(envelope("one"), "topic", "a.b")
        await channel.publish(envelope("two"), "topic", "a.b.c")

        assert (await channel.get("star")).body == b"one"
        assert await channel.get("star") is None

    async def test_payload_that_is_not_an_envelope_is_delivered_raw(self, options, caplog):
        channel = await make_channel(options)
        await channel.publish(b"[1, 2, 3]", "", "q")

        with caplog.at_level(logging.ERROR, logger="kombu.transport.base"):
            message = await channel.get("q")

        assert message.body == b"[1, 2, 3]"
        assert message.content_type == "application/data"
        assert "q" in caplog.text


class test_bindings:
    async def test_a_binding_made_elsewhere_survives(self, options, control_folder):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("mine", "fan")
        # A binding another worker wrote while this one was not looking.
        control_file = control_folder / "fan.exchange"
        control_file.write_text(json_dumps([*json_loads(control_file.read_text()), ["", "theirs"]]))

        await channel.queue_bind("later", "fan")
        await channel.publish(envelope("hi"), "fan", "")

        assert [b[1] for b in json_loads(control_file.read_text())] == ["mine", "theirs", "later"]
        assert (await channel.get("theirs")).body == b"hi"
        assert (await channel.get("later")).body == b"hi"

    async def test_two_transports_bind_to_the_same_exchange(self, options, control_folder):
        first = await make_channel(options)
        second = await make_channel(options)
        await first.declare_exchange(Exchange("fan", type="fanout"))
        await second.declare_exchange(Exchange("fan", type="fanout"))

        await first.queue_bind("one", "fan")
        await second.queue_bind("two", "fan")

        assert json_loads((control_folder / "fan.exchange").read_text()) == [["", "one"], ["", "two"]]
        await first.publish(envelope("hi"), "fan", "")
        assert (await second.get("one")).body == b"hi"
        assert (await second.get("two")).body == b"hi"

    async def test_unbind_removes_only_that_binding(self, options):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")
        await channel.queue_bind("two", "fan")

        await channel.queue_unbind("one", "fan")
        await channel.publish(envelope("hi"), "fan", "")

        assert await channel.get("one") is None
        assert (await channel.get("two")).body == b"hi"

    async def test_a_corrupt_control_file_is_reported(self, options, control_folder):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")
        (control_folder / "fan.exchange").write_text("{not json")

        with pytest.raises(ChannelError, match="fan"):
            await channel.publish(envelope("hi"), "fan", "")

    async def test_a_control_file_that_is_not_text_is_reported(self, options, control_folder):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")
        (control_folder / "fan.exchange").write_bytes(b"\xff\xfe\x00")

        with pytest.raises(ChannelError, match="fan"):
            await channel.publish(envelope("hi"), "fan", "")

    async def test_deleting_an_exchange_removes_its_control_file(self, options, control_folder):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")

        await channel.exchange_delete("fan")

        assert not (control_folder / "fan.exchange").exists()

    async def test_deleting_a_queue_drops_its_bindings(self, options):
        channel = await make_channel(options)
        await channel.declare_exchange(Exchange("fan", type="fanout"))
        await channel.queue_bind("one", "fan")
        await channel.queue_bind("two", "fan")

        await channel.queue_delete("one")
        await channel.publish(envelope("hi"), "fan", "")

        assert await channel.get("one") is None
        assert (await channel.get("two")).body == b"hi"


class test_queue_management:
    async def test_purge_returns_the_number_dropped(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("one"), "", "q")
        await channel.publish(envelope("two"), "", "q")

        assert await channel.queue_purge("q") == 2
        assert await channel.get("q") is None

    async def test_delete_if_empty_keeps_a_queue_with_messages(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")

        assert await channel.queue_delete("q", if_empty=True) == 0
        assert (await channel.get("q")).body == b"hi"

    async def test_purge_leaves_messages_that_are_being_worked_on(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        assert await channel.queue_purge("q") == 0

        await channel.basic_reject(message.delivery_tag, requeue=True)
        assert (await channel.get("q")).body == b"hi"


class test_acknowledgement:
    async def test_requeue_restores_the_message(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await message.requeue()

        assert (await channel.get("q")).body == b"hi"

    async def test_reject_without_requeue_drops_the_message(self, options, data_folder):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await channel.basic_reject(message.delivery_tag, requeue=False)

        assert await channel.get("q") is None
        assert list((data_folder / INFLIGHT_DIR).iterdir()) == []

    async def test_recover_restores_everything_outstanding(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("one"), "", "q")
        await channel.publish(envelope("two"), "", "q")
        await channel.get("q")
        await channel.get("q")

        await channel.basic_recover(requeue=True)

        assert (await channel.get("q")).body == b"one"
        assert (await channel.get("q")).body == b"two"

    async def test_ack_removes_the_file(self, options, data_folder):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await message.ack()

        assert list((data_folder / INFLIGHT_DIR).iterdir()) == []
        assert await channel.get("q") is None

    async def test_ack_keeps_the_file_when_store_processed_is_set(self, options, tmp_path):
        channel = await make_channel(options, store_processed=True)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await message.ack()

        assert [p.read_bytes() for p in (tmp_path / "processed").iterdir()] == [envelope("hi")]

    async def test_ack_multiple_of_an_unknown_tag_keeps_the_rest(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        message = await channel.get("q")

        await channel.basic_ack("bogus-tag", multiple=True)

        assert message.delivery_tag in channel._unacked
        await channel.basic_reject(message.delivery_tag, requeue=True)
        assert (await channel.get("q")).body == b"hi"

    async def test_a_no_ack_get_leaves_nothing_in_flight(self, options, data_folder):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")

        message = await channel.get("q", no_ack=True)

        assert message.body == b"hi"
        assert channel._unacked == {}
        assert list((data_folder / INFLIGHT_DIR).iterdir()) == []

    async def test_close_requeues_what_was_in_flight(self, options):
        channel = await make_channel(options)
        await channel.publish(envelope("hi"), "", "q")
        await channel.get("q")

        await channel.close()

        assert (await (await make_channel(options)).get("q")).body == b"hi"


class test_drain_events:
    async def test_delivers_a_queued_message(self, options):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("hi"), "", "q")

        assert await channel.drain_events(timeout=1) is True
        assert received == ["hi"]

    async def test_timeout_zero_polls_without_blocking(self, options):
        channel = await make_channel(options)
        await channel.basic_consume("q", lambda body, message: None)

        started = time.monotonic()
        assert await channel.drain_events(timeout=0) is False
        assert time.monotonic() - started < 0.5

    async def test_timeout_zero_delivers_what_is_ready(self, options):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("hi"), "", "q")

        started = time.monotonic()
        assert await channel.drain_events(timeout=0) is True
        assert time.monotonic() - started < 0.5
        assert received == ["hi"]

    async def test_a_short_timeout_is_honoured(self, options):
        channel = await make_channel(options)
        await channel.basic_consume("q", lambda body, message: None)

        started = time.monotonic()
        assert await channel.drain_events(timeout=0.3) is False
        elapsed = time.monotonic() - started
        assert 0.3 <= elapsed < 0.8

    async def test_a_timeout_longer_than_the_poll_interval_is_honoured(self, options):
        channel = await make_channel(options)
        await channel.basic_consume("q", lambda body, message: None)

        started = time.monotonic()
        assert await channel.drain_events(timeout=1.2) is False
        assert time.monotonic() - started >= 1.2

    async def test_a_message_published_while_waiting_is_picked_up(self, options):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))

        async def publish_soon():
            await asyncio.sleep(POLL_INTERVAL)
            await channel.publish(envelope("hi"), "", "q")

        publisher = asyncio.create_task(publish_soon())
        assert await channel.drain_events(timeout=5) is True
        await publisher

        assert received == ["hi"]

    async def test_cancelling_a_consumer_during_a_drain(self, options):
        channel = await make_channel(options)
        tag = await channel.basic_consume("q", lambda body, message: None)

        async def cancel_soon():
            await asyncio.sleep(0)
            await channel.basic_cancel(tag)

        canceller = asyncio.create_task(cancel_soon())
        assert await channel.drain_events(timeout=0.2) is False
        await canceller

    async def test_cancelling_a_drain_leaves_the_message_queued(self, options):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(channel.drain_events(timeout=5), 0.1)

        await channel.publish(envelope("hi"), "", "q")

        assert await channel.drain_events(timeout=1) is True
        assert received == ["hi"]

    async def test_a_body_that_cannot_be_deserialized_is_passed_on(self, options, caplog):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("q", lambda body, message: received.append(body))
        await channel.publish(envelope("{truncated", **{"content-type": "application/json"}), "", "q")

        with caplog.at_level(logging.WARNING, logger="kombu.transport.filesystem"):
            assert await channel.drain_events(timeout=1) is True

        assert received == [b"{truncated"]
        assert "Cannot decode message" in caplog.text

    async def test_a_busy_queue_does_not_starve_the_others(self, options):
        channel = await make_channel(options)
        received = []
        await channel.basic_consume("busy", lambda body, message: received.append(("busy", body)))
        await channel.basic_consume("quiet", lambda body, message: received.append(("quiet", body)))
        await channel.publish(envelope("reply"), "", "quiet")

        for _ in range(3):
            await channel.publish(envelope("task"), "", "busy")
            assert await channel.drain_events(timeout=1) is True

        assert ("quiet", "reply") in received

    async def test_no_consumers_still_honours_the_timeout(self, options):
        channel = await make_channel(options)

        started = time.monotonic()
        assert await channel.drain_events(timeout=0.2) is False
        assert time.monotonic() - started >= 0.2
