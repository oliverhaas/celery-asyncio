import asyncio
import logging
import socket
import time
from unittest.mock import Mock, call

import pytest

from celery.events import Event
from celery.events.receiver import CLIENT_CLOCK_SKEW
from celery.exceptions import ImproperlyConfigured


class MockProducer:
    """Stand-in for kombu's producer, whose `publish` is a coroutine."""

    raise_on_publish = False

    def __init__(self, *args, **kwargs):
        self.sent = []

    async def publish(self, msg, *args, **kwargs):
        if self.raise_on_publish:
            raise KeyError()
        self.sent.append(msg)

    def close(self):
        pass

    def has_event(self, kind):
        for event in self.sent:
            if event["type"] == kind:
                return event
        return False


async def settle():
    """Let the scheduled publish tasks and their done callbacks run."""
    for _ in range(3):
        await asyncio.sleep(0)


def test_Event():
    event = Event("world war II")
    assert event["type"] == "world war II"
    assert event["timestamp"]


class test_EventDispatcher:
    def test_redis_uses_fanout_exchange(self):
        self.app.connection = Mock()
        conn = self.app.connection.return_value = Mock()
        conn.transport.driver_type = "redis"

        dispatcher = self.app.events.Dispatcher(conn, enabled=False)
        assert dispatcher.exchange.type == "fanout"

    def test_others_use_topic_exchange(self):
        self.app.connection = Mock()
        conn = self.app.connection.return_value = Mock()
        conn.transport.driver_type = "amqp"
        dispatcher = self.app.events.Dispatcher(conn, enabled=False)
        assert dispatcher.exchange.type == "topic"

    def test_takes_channel_connection(self):
        x = self.app.events.Dispatcher(channel=Mock())
        assert x.connection is x.channel.connection.client

    def test_sql_transports_disabled(self):
        conn = Mock()
        conn.transport.driver_type = "sql"
        x = self.app.events.Dispatcher(connection=conn)
        assert not x.enabled

    async def test_send(self):
        producer = MockProducer()
        producer.connection = self.app.connection_for_write()
        connection = Mock()
        connection.transport.driver_type = "amqp"
        eventer = self.app.events.Dispatcher(connection, enabled=False, buffer_while_offline=False)
        eventer.producer = producer
        eventer.enabled = True
        eventer.send("World War II", ended=True)
        await settle()
        assert producer.has_event("World War II")
        eventer.enabled = False
        eventer.send("World War III")
        await settle()
        assert not producer.has_event("World War III")

        evs = ("Event 1", "Event 2", "Event 3")
        eventer.enabled = True
        eventer.producer.raise_on_publish = True
        eventer.buffer_while_offline = False
        eventer.send("Event X")
        await settle()
        assert not producer.has_event("Event X")
        assert not eventer._outbound_buffer

        eventer.buffer_while_offline = True
        for ev in evs:
            eventer.send(ev)
        await settle()
        assert [routing_key for _, routing_key in eventer._outbound_buffer] == list(evs)

        eventer.producer.raise_on_publish = False
        eventer.flush()
        await settle()
        for ev in evs:
            assert producer.has_event(ev)
        assert not eventer._outbound_buffer

    async def test_send_buffers_what_the_publish_task_failed_to_deliver(self, caplog):
        # The publish is a coroutine, so it cannot have failed by the time
        # `send` returns: the buffering used to sit in an except around the
        # call, where nothing ever raised, and every event lost while the
        # broker was down was lost for good.
        producer = MockProducer()
        producer.raise_on_publish = True
        eventer = self.app.events.Dispatcher(Mock(), enabled=False, buffer_while_offline=True)
        eventer.producer = producer
        eventer.enabled = True

        with caplog.at_level(logging.WARNING, logger="celery.events.dispatcher"):
            eventer.send("task-sent", uuid=1)
            await settle()

        assert [routing_key for _, routing_key in eventer._outbound_buffer] == ["task.sent"]
        assert "task.sent buffered" in caplog.text

    async def test_send_without_buffering_warns_about_the_dropped_event(self, caplog):
        producer = MockProducer()
        producer.raise_on_publish = True
        eventer = self.app.events.Dispatcher(Mock(), enabled=False, buffer_while_offline=False)
        eventer.producer = producer
        eventer.enabled = True

        with caplog.at_level(logging.WARNING, logger="celery.events.dispatcher"):
            eventer.send("task-sent", uuid=1)
            await settle()

        assert not eventer._outbound_buffer
        assert "task.sent dropped" in caplog.text

    def test_send_from_a_thread_without_a_loop_reaches_the_broker(self):
        # A dispatcher built by a client, outside any loop, has no loop of its
        # own to publish on. It used to throw the coroutine away unexecuted,
        # which made every client-side task-sent event a no-op.
        producer = MockProducer()
        eventer = self.app.events.Dispatcher(Mock(), enabled=False)
        eventer.producer = producer
        eventer.enabled = True
        assert eventer._event_loop is None

        eventer.send("task-sent", uuid=1)

        deadline = time.monotonic() + 5
        while not producer.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert producer.has_event("task-sent")

    def test_send_buffer_group(self):
        buf_received = [None]
        producer = MockProducer()
        producer.connection = self.app.connection_for_write()
        connection = Mock()
        connection.transport.driver_type = "amqp"
        eventer = self.app.events.Dispatcher(
            connection,
            enabled=False,
            buffer_group={"task"},
            buffer_limit=2,
        )
        eventer.producer = producer
        eventer.enabled = True
        eventer._publish = Mock(name="_publish")

        def on_eventer_publish(events, *args, **kwargs):
            buf_received[0] = list(events)

        eventer._publish.side_effect = on_eventer_publish
        assert not eventer._group_buffer["task"]
        eventer.on_send_buffered = Mock(name="on_send_buffered")
        eventer.send("task-received", uuid=1)
        prev_buffer = eventer._group_buffer["task"]
        assert eventer._group_buffer["task"]
        eventer.on_send_buffered.assert_called_with()
        eventer.send("task-received", uuid=1)
        assert not eventer._group_buffer["task"]
        # The payload is a detached copy, so it still holds both events after
        # the live buffer was cleared. It used to be the live list itself, which
        # meant the recorded call was [] and, since publishing is a coroutine
        # here, so was what actually went to the broker.
        (published_events, published_producer, published_routing_key) = eventer._publish.call_args[0]
        assert len(published_events) == 2
        assert published_producer is eventer.producer
        assert published_routing_key == "task.multi"
        assert published_events is not prev_buffer
        # clear in place
        assert eventer._group_buffer["task"] is prev_buffer
        assert len(buf_received[0]) == 2
        eventer.on_send_buffered = None
        eventer.send("task-received", uuid=1)

    def test_flush_no_groups_no_errors(self):
        eventer = self.app.events.Dispatcher(Mock())
        eventer.flush(errors=False, groups=False)

    def test_group_flush_keeps_events_appended_during_the_publish(self):
        # Appends that land while a publish is in flight belong to the next
        # flush. Clearing the whole list destroyed them silently.
        eventer = self.app.events.Dispatcher(Mock(), enabled=False, buffer_group={"task"}, buffer_limit=100)
        eventer.producer = MockProducer()
        eventer.enabled = True
        buf = eventer._group_buffer["task"]

        def append_during_publish(events, *args, **kwargs):
            buf.append(Event("task-succeeded", uuid=99))

        eventer._publish = Mock(name="_publish", side_effect=append_during_publish)
        eventer.send("task-received", uuid=1)
        eventer.send("task-received", uuid=2)

        eventer.flush()

        assert len(eventer._publish.call_args[0][0]) == 2
        assert [e["uuid"] for e in buf] == [99]

    def test_error_flush_keeps_entries_that_failed_to_republish(self):
        # _publish re-buffers what it could not send, and clearing the buffer
        # after the replay threw that away again.
        eventer = self.app.events.Dispatcher(Mock(), enabled=False)
        eventer.producer = MockProducer()
        eventer.enabled = True
        eventer._outbound_buffer.append((Event("task-sent", uuid=1), "task.sent"))

        def rebuffer(event, producer, routing_key, **kwargs):
            eventer._outbound_buffer.append((event, routing_key))

        eventer._publish = Mock(name="_publish", side_effect=rebuffer)

        eventer.flush(groups=False)

        assert len(eventer._outbound_buffer) == 1

    async def test_buffered_entries_do_not_hold_on_to_the_exception(self):
        # The exception pins its traceback and every frame below it, which for a
        # dispatcher retrying against a dead broker is the leak.
        producer = MockProducer()
        producer.raise_on_publish = True
        eventer = self.app.events.Dispatcher(Mock(), enabled=False, buffer_while_offline=True)
        eventer.producer = producer
        eventer.enabled = True

        eventer.send("task-sent", uuid=1)
        await settle()

        (entry,) = eventer._outbound_buffer
        assert len(entry) == 2
        assert not any(isinstance(part, BaseException) for part in entry)

    async def test_publish_without_a_producer_is_a_no_op(self):
        # Stale timers keep calling a closed dispatcher after a reconnect. Each
        # call used to raise AttributeError and land in the buffer.
        eventer = self.app.events.Dispatcher(Mock(), enabled=False, buffer_while_offline=True)
        eventer.enabled = True
        eventer.producer = None

        eventer.send("worker-heartbeat")
        await settle()

        assert not eventer._outbound_buffer

    def test_enter_exit(self):
        conn = self.app.connection_for_write()
        d = self.app.events.Dispatcher(conn)
        d.close = Mock()
        with d as _d:
            assert _d
        d.close.assert_called_with()

    def test_enable_disable_callbacks(self):
        on_enable = Mock()
        on_disable = Mock()
        conn = self.app.connection_for_write()
        with self.app.events.Dispatcher(conn, enabled=False) as d:
            d.on_enabled.add(on_enable)
            d.on_disabled.add(on_disable)
            d.enable()
            on_enable.assert_called_with()
            d.disable()
            on_disable.assert_called_with()

    @pytest.mark.skip(reason="EventDispatcher uses sync producer; needs async refactor")
    def test_enabled_disable(self):
        connection = self.app.connection_for_write()
        channel = connection.channel()
        try:
            dispatcher = self.app.events.Dispatcher(connection, enabled=True)
            dispatcher2 = self.app.events.Dispatcher(connection, enabled=True, channel=channel)
            assert dispatcher.enabled
            assert dispatcher.producer.channel
            assert dispatcher.producer.serializer == self.app.conf.event_serializer

            created_channel = dispatcher.producer.channel
            dispatcher.disable()
            dispatcher.disable()  # Disable with no active producer
            dispatcher2.disable()
            assert not dispatcher.enabled
            assert dispatcher.producer is None
            # does not close manually provided channel
            assert not dispatcher2.channel.closed

            dispatcher.enable()
            assert dispatcher.enabled
            assert dispatcher.producer

        finally:
            channel.close()
            connection.close()
        assert created_channel.closed


class test_EventReceiver:
    def test_process(self):
        message = {"type": "world-war"}

        got_event = [False]

        def my_handler(event):
            got_event[0] = True

        connection = Mock()
        connection.transport_cls = "memory"
        r = self.app.events.Receiver(
            connection,
            handlers={"world-war": my_handler},
            node_id="celery.tests",
        )
        r._receive(message, object())
        assert got_event[0]

    def test_accept_argument(self):
        r = self.app.events.Receiver(Mock(), accept={"app/foo"})
        assert r.accept == {"app/foo"}

    def test_event_queue_prefix__default(self):
        r = self.app.events.Receiver(Mock())
        assert r.queue.name.startswith("celeryev.")

    def test_event_queue_prefix__setting(self):
        self.app.conf.event_queue_prefix = "eventq"
        r = self.app.events.Receiver(Mock())
        assert r.queue.name.startswith("eventq.")

    def test_event_queue_prefix__argument(self):
        r = self.app.events.Receiver(Mock(), queue_prefix="fooq")
        assert r.queue.name.startswith("fooq.")

    def test_event_exchange__default(self):
        r = self.app.events.Receiver(Mock())
        assert r.exchange.name == "celeryev"

    def test_event_exchange__setting(self):
        self.app.conf.event_exchange = "exchange_ev"
        r = self.app.events.Receiver(Mock())
        assert r.exchange.name == "exchange_ev"

    def test_catch_all_event(self):
        message = {"type": "world-war"}
        got_event = [False]

        def my_handler(event):
            got_event[0] = True

        connection = Mock()
        connection.transport_cls = "memory"
        r = self.app.events.Receiver(connection, node_id="celery.tests")
        r.handlers["*"] = my_handler
        r._receive(message, object())
        assert got_event[0]

    @pytest.mark.skip(reason="EventReceiver uses sync ConsumerMixin; needs async refactor")
    def test_itercapture(self):
        connection = self.app.connection_for_write()
        try:
            r = self.app.events.Receiver(connection, node_id="celery.tests")
            it = r.itercapture(timeout=0.0001, wakeup=False)

            with pytest.raises(socket.timeout):
                next(it)

            with pytest.raises(socket.timeout):
                r.capture(timeout=0.00001)
        finally:
            connection.close()

    def test_event_from_message_localize_disabled(self):
        r = self.app.events.Receiver(Mock(), node_id="celery.tests")
        r.adjust_clock = Mock()
        ts_adjust = Mock()

        r.event_from_message(
            {"type": "worker-online", "clock": 313},
            localize=False,
            adjust_timestamp=ts_adjust,
        )
        ts_adjust.assert_not_called()
        r.adjust_clock.assert_called_with(313)

    def test_event_from_message_clock_from_client(self):
        r = self.app.events.Receiver(Mock(), node_id="celery.tests")
        r.clock.value = 302
        r.adjust_clock = Mock()

        body = {"type": "task-sent"}
        r.event_from_message(
            body,
            localize=False,
            adjust_timestamp=Mock(),
        )
        assert body["clock"] == r.clock.value + CLIENT_CLOCK_SKEW

    def test_receive_multi(self):
        r = self.app.events.Receiver(Mock(name="connection"))
        r.process = Mock(name="process")
        efm = r.event_from_message = Mock(name="event_from_message")

        def on_efm(*args):
            return args

        efm.side_effect = on_efm
        r._receive([1, 2, 3], Mock())
        r.process.assert_has_calls([call(1), call(2), call(3)])

    @pytest.mark.skip(reason="EventReceiver uses sync ConsumerMixin; needs async refactor")
    def test_itercapture_limit(self):
        connection = self.app.connection_for_write()
        channel = connection.channel()
        try:
            events_received = [0]

            def handler(event):
                events_received[0] += 1

            producer = self.app.events.Dispatcher(
                connection,
                enabled=True,
                channel=channel,
            )
            r = self.app.events.Receiver(
                connection,
                handlers={"*": handler},
                node_id="celery.tests",
            )
            evs = ["ev1", "ev2", "ev3", "ev4", "ev5"]
            for ev in evs:
                producer.send(ev)
            it = r.itercapture(limit=4, wakeup=True)
            next(it)  # skip consumer (see itercapture)
            list(it)
            assert events_received[0] == 4
        finally:
            channel.close()
            connection.close()

    def test_event_queue_is_exclusive_by_default(self):
        # RabbitMQ 4.3.0 refuses transient non-exclusive queues.
        q = self.app.events.Receiver(Mock(name="connection")).queue

        assert q.exclusive is True
        assert q.durable is False

    def test_asking_only_for_a_durable_event_queue_still_works(self):
        # The new default must not turn into an ImproperlyConfigured for a
        # caller that only asked for durability.
        self.app.conf.update(event_queue_durable=True)
        q = self.app.events.Receiver(Mock(name="connection")).queue

        assert q.durable is True
        assert q.exclusive is False

    def test_event_queue_exclusive(self):
        self.app.conf.update(event_queue_exclusive=True, event_queue_durable=False)

        ev_recv = self.app.events.Receiver(Mock(name="connection"))
        q = ev_recv.queue

        assert q.exclusive is True
        assert q.durable is False
        assert q.auto_delete is True

    def test_event_queue_durable_and_validation(self):
        self.app.conf.update(event_queue_exclusive=False, event_queue_durable=True)
        ev_recv = self.app.events.Receiver(Mock(name="connection"))
        q = ev_recv.queue

        assert q.durable is True
        assert q.exclusive is False
        assert q.auto_delete is False

        self.app.conf.update(event_queue_exclusive=True, event_queue_durable=True)

        with pytest.raises(ImproperlyConfigured):
            self.app.events.Receiver(Mock(name="connection"))


def test_State(app):
    state = app.events.State()
    assert dict(state.workers) == {}


@pytest.mark.skip(reason="default_dispatcher uses producer_pool; needs async refactor")
def test_default_dispatcher(app):
    with app.events.default_dispatcher() as d:
        assert d
        assert d.connection


class DummyConn:
    class transport:
        driver_type = "amqp"


def test_get_exchange_default_type():
    from celery.events import event

    conn = DummyConn()
    ex = event.get_exchange(conn)
    assert ex.type == "topic"
    assert ex.name == event.EVENT_EXCHANGE_NAME


def test_get_exchange_redis_type():
    from celery.events import event

    class RedisConn:
        class transport:
            driver_type = "redis"

    conn = RedisConn()
    ex = event.get_exchange(conn)
    assert ex.type == "fanout"
    assert ex.name == event.EVENT_EXCHANGE_NAME


def test_get_exchange_custom_name():
    from celery.events import event

    conn = DummyConn()
    ex = event.get_exchange(conn, name="custom")
    assert ex.name == "custom"


def test_group_from():
    from celery.events import event

    print("event.py loaded from:", event.__file__)
    assert event.group_from("task-sent") == "task"
    assert event.group_from("custom-my-event") == "custom"
    assert event.group_from("foo") == "foo"
