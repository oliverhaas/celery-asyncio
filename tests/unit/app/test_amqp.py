from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from kombu import Exchange, Queue

from celery import uuid
from celery.app.amqp import Queues, utf8dict
from celery.utils.time import to_utc


class test_Queues:
    def test_queues_format(self):
        self.app.amqp.queues._consume_from = {}
        assert self.app.amqp.queues.format() == ""

    def test_with_defaults(self):
        assert Queues(None) == {}

    def test_add(self):
        q = Queues()
        q.add("foo", exchange="ex", routing_key="rk")
        assert "foo" in q
        assert isinstance(q["foo"], Queue)
        assert q["foo"].routing_key == "rk"

    def test_add_compat_takes_binding_key_as_the_routing_key(self):
        q = Queues()
        q.add_compat("foo", binding_key="rk")
        assert q["foo"].routing_key == "rk"

    def test_setitem_adds_default_exchange(self):
        q = Queues(default_exchange=Exchange("bar"))
        assert q.default_exchange
        queue = Queue("foo", exchange=None)
        queue.exchange = None
        q["foo"] = queue
        assert q["foo"].exchange == q.default_exchange

    def test_select_add(self):
        q = Queues()
        q.select(["foo", "bar"])
        q.select_add("baz")
        assert sorted(q._consume_from.keys()) == ["bar", "baz", "foo"]

    def test_deselect(self):
        q = Queues()
        q.select(["foo", "bar"])
        q.deselect("bar")
        assert sorted(q._consume_from.keys()) == ["foo"]

    def test_select_keys_by_the_real_name_not_the_alias(self):
        # An alias key does not match the q.name key select_add writes, so the
        # queue ended up in consume_from twice and was consumed twice.
        q = Queues()
        q.add(Queue("real-name", alias="short"))
        q.select(["short"])
        q.select_add(Queue("real-name", alias="short"))
        assert sorted(q.consume_from) == ["real-name"]

    def test_routing_only_queues_are_not_consumed_from(self):
        # __missing__ adds every queue a task is merely routed to. Those must
        # not turn into things the worker consumes on its next reconnect.
        q = Queues(queues=[Queue("declared")])
        assert q["routed-to"]
        assert sorted(q.consume_from) == ["declared"]

    def test_select_add_without_a_selection_reaches_consumers(self):
        q = Queues(queues=[Queue("declared")])
        q.select_add(Queue("worker-direct"))
        assert sorted(q.consume_from) == ["declared", "worker-direct"]

    def test_deselect_without_a_selection_keeps_routing_only_queues_out(self):
        # deselect() used to promote the whole routing table to an explicit
        # selection, which pulled the routing-only queue in with it.
        q = Queues(queues=[Queue("a"), Queue("b")])
        assert q["routed-to"]
        q.deselect(["b"])
        assert sorted(q.consume_from) == ["a"]

    def test_add_default_exchange(self):
        ex = Exchange("fff", "fanout")
        q = Queues(default_exchange=ex)
        q.add(Queue("foo"))
        assert q["foo"].exchange.name == "fff"

    def test_alias(self):
        q = Queues()
        q.add(Queue("foo", alias="barfoo"))
        assert q["barfoo"] is q["foo"]

    @pytest.mark.parametrize(
        "queues_kwargs,qname,q,expected",
        [
            ({"max_priority": 10}, "foo", "foo", {"x-max-priority": 10}),
            ({"max_priority": 10}, "xyz", Queue("xyz", queue_arguments={"x-max-priority": 3}), {"x-max-priority": 3}),
            ({"max_priority": 10}, "moo", Queue("moo", queue_arguments=None), {"x-max-priority": 10}),
            ({"max_priority": None}, "foo2", "foo2", {}),
            (
                {"max_priority": None},
                "xyx3",
                Queue("xyx3", queue_arguments={"x-max-priority": 7}),
                {"x-max-priority": 7},
            ),
        ],
    )
    def test_with_max_priority(self, queues_kwargs, qname, q, expected):
        queues = Queues(**queues_kwargs)
        queues.add(q)
        assert queues[qname].queue_arguments == expected

    def test_missing_queue_quorum(self):
        queues = Queues(create_missing_queue_type="quorum", create_missing_queue_exchange_type="topic")

        q = queues.new_missing("spontaneous")
        assert q.name == "spontaneous"
        assert q.queue_arguments == {"x-queue-type": "quorum"}
        assert q.exchange.type == "topic"


class test_default_queues:
    @pytest.mark.parametrize("default_queue_type", ["classic", "quorum"])
    @pytest.mark.parametrize(
        "name,exchange,rkey",
        [
            ("default", None, None),
            ("default", "exchange", None),
            ("default", "exchange", "routing_key"),
            ("default", None, "routing_key"),
        ],
    )
    def test_setting_default_queue(self, name, exchange, rkey, default_queue_type):
        self.app.conf.task_queues = {}
        self.app.conf.task_default_exchange = exchange
        self.app.conf.task_default_routing_key = rkey
        self.app.conf.task_default_queue = name
        self.app.conf.task_default_queue_type = default_queue_type
        assert self.app.amqp.queues.default_exchange.name == exchange or name
        queues = dict(self.app.amqp.queues)
        assert len(queues) == 1
        queue = queues[name]
        assert queue.exchange.name == exchange or name
        assert queue.exchange.type == "direct"
        assert queue.routing_key == rkey or name

        if default_queue_type == "quorum":
            assert queue.queue_arguments == {"x-queue-type": "quorum"}
        else:
            assert not queue.queue_arguments  # {} or None


class test_default_exchange:
    @pytest.mark.parametrize(
        "name,exchange,rkey",
        [
            ("default", "foo", None),
            ("default", "foo", "routing_key"),
        ],
    )
    def test_setting_default_exchange(self, name, exchange, rkey):
        q = Queue(name, routing_key=rkey)
        self.app.conf.task_queues = {q}
        self.app.conf.task_default_exchange = exchange
        queues = dict(self.app.amqp.queues)
        queue = queues[name]
        assert queue.exchange.name == exchange

    @pytest.mark.parametrize(
        "name,extype,rkey",
        [
            ("default", "direct", None),
            ("default", "direct", "routing_key"),
            ("default", "topic", None),
            ("default", "topic", "routing_key"),
        ],
    )
    def test_setting_default_exchange_type(self, name, extype, rkey):
        q = Queue(name, routing_key=rkey)
        self.app.conf.task_queues = {q}
        self.app.conf.task_default_exchange_type = extype
        queues = dict(self.app.amqp.queues)
        queue = queues[name]
        assert queue.exchange.type == extype


class test_AMQP_proto1:
    def test_kwargs_must_be_mapping(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v1(uuid(), "foo", kwargs=[1, 2])

    def test_args_must_be_list(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v1(uuid(), "foo", args="abc")

    def test_countdown_negative(self):
        with pytest.raises(ValueError):
            self.app.amqp.as_task_v1(uuid(), "foo", countdown=-1232132323123)

    def test_as_task_message_without_utc(self):
        self.app.amqp.utc = False
        self.app.amqp.as_task_v1(uuid(), "foo", countdown=30, expires=40)


class test_AMQP_Base:
    def setup_method(self):
        self.simple_message = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            create_sent_event=True,
        )
        self.simple_message_no_sent_event = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            create_sent_event=False,
        )

    def producer(self):
        """A producer whose publish and channel lookup can be awaited."""
        prod = Mock(name="producer")
        prod.publish = AsyncMock(name="publish")
        prod._ensure_channel = AsyncMock(name="_ensure_channel")
        return prod


class test_AMQP(test_AMQP_Base):
    def test_kwargs_must_be_mapping(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), "foo", kwargs=[1, 2])

    def test_args_must_be_list(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), "foo", args="abc")

    def test_countdown_negative(self):
        with pytest.raises(ValueError):
            self.app.amqp.as_task_v2(uuid(), "foo", countdown=-1232132323123)

    def test_Queues__with_max_priority(self):
        x = self.app.amqp.Queues({}, max_priority=23)
        assert x.max_priority == 23

    async def test_send_task_message__no_kwargs(self):
        await self.app.amqp.asend_task_message(self.producer(), "foo", self.simple_message)

    async def test_send_task_message__properties(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            foo=1,
            retry=False,
        )
        assert prod.publish.call_args[1]["foo"] == 1

    async def test_send_task_message__publish_options_are_not_properties(self):
        # Anything the sender does not name lands in the message properties,
        # and these two steer the publish rather than describe the message.
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            retry=True,
            retry_policy={"max_retries": 5},
        )
        kwargs = prod.publish.call_args[1]
        assert kwargs["retry"] is True
        assert kwargs["retry_policy"]["max_retries"] == 5
        assert "retry" not in self.simple_message_no_sent_event[1]

    async def test_send_task_message__headers(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            headers={"x1x": "y2x"},
            retry=False,
        )
        assert prod.publish.call_args[1]["headers"]["x1x"] == "y2x"

    async def test_send_task_message__queue_string(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            queue="foo",
            retry=False,
        )
        kwargs = prod.publish.call_args[1]
        assert kwargs["routing_key"] == "foo"
        assert kwargs["exchange"] == ""

    async def test_send_task_message__declares_the_queue(self):
        # A task sent before the worker ever ran went to a queue that did not
        # exist yet, and the broker dropped it.
        prod = self.producer()
        channel = prod._ensure_channel.return_value
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            queue="foo",
        )
        assert channel.declare_queue.await_args.args[0].name == "foo"

    async def test_send_task_message__broadcast_without_exchange(self):
        from kombu.common import Broadcast

        evd = Mock(name="evd")
        await self.app.amqp.asend_task_message(
            self.producer(),
            "foo",
            self.simple_message,
            retry=False,
            routing_key="xyz",
            queue=Broadcast("abc"),
            event_dispatcher=evd,
        )
        evd.publish.assert_called()
        event = evd.publish.call_args[0][1]
        assert event["routing_key"] == "xyz"
        assert event["exchange"] == "abc"

    async def test_send_event_exchange_direct_with_exchange(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            queue="bar",
            retry=False,
            exchange_type="direct",
            exchange="xyz",
        )
        prod.publish.assert_called()
        pub = prod.publish.call_args[1]
        assert pub["routing_key"] == "bar"
        assert pub["exchange"] == ""

    async def test_send_event_exchange_direct_with_routing_key(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            queue="bar",
            retry=False,
            exchange_type="direct",
            routing_key="xyb",
        )
        prod.publish.assert_called()
        pub = prod.publish.call_args[1]
        assert pub["routing_key"] == "bar"
        assert pub["exchange"] == ""

    async def test_send_event_exchange_string(self):
        evd = Mock(name="evd")
        await self.app.amqp.asend_task_message(
            self.producer(),
            "foo",
            self.simple_message,
            retry=False,
            exchange="xyz",
            routing_key="xyb",
            event_dispatcher=evd,
        )
        evd.publish.assert_called()
        event = evd.publish.call_args[0][1]
        assert event["routing_key"] == "xyb"
        assert event["exchange"] == "xyz"

    async def test_send_task_message__no_default_queue(self):
        # Reading amqp.default_queue creates it. Capturing it when the sender
        # was built therefore raised KeyError for a setup that routes every
        # task explicitly and has missing-queue creation turned off.
        conf = self.app.conf
        conf.task_create_missing_queues = False
        conf.task_queues = {Queue("my_queue")}

        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            queue="my_queue",
            retry=False,
        )
        kwargs = prod.publish.call_args[1]
        assert kwargs["routing_key"] == "my_queue"
        assert kwargs["exchange"] == ""

    async def test_send_task_message__with_delivery_mode(self):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.simple_message_no_sent_event,
            delivery_mode=33,
            retry=False,
        )
        assert prod.publish.call_args[1]["delivery_mode"] == 33

    def test_routes(self):
        r1 = self.app.amqp.routes
        r2 = self.app.amqp.routes
        assert r1 is r2

    def update_conf_runtime_for_tasks_queues(self):
        self.app.conf.update(task_routes={"task.create_pr": "queue.qwerty"})
        self.app.send_task("task.create_pr")
        router_was = self.app.amqp.router
        self.app.conf.update(task_routes={"task.create_pr": "queue.asdfgh"})
        self.app.send_task("task.create_pr")
        router = self.app.amqp.router
        assert router != router_was

    def test_create_missing_queue_type_from_conf(self):
        self.app.conf.task_create_missing_queue_type = "quorum"
        self.app.conf.task_create_missing_queue_exchange_type = "topic"
        self.app.amqp.__dict__.pop("queues", None)
        q = self.app.amqp.queues["auto"]
        assert q.queue_arguments == {"x-queue-type": "quorum"}
        assert q.exchange.type == "topic"

    def test_create_missing_queue_type_explicit_param(self):
        qmap = self.app.amqp.Queues(
            {}, create_missing=True, create_missing_queue_type="quorum", create_missing_queue_exchange_type="topic"
        )
        q = qmap["auto"]
        assert q.queue_arguments == {"x-queue-type": "quorum"}
        assert q.exchange.type == "topic"


class test_eta_property(test_AMQP_Base):
    """The eta a transport can act on without knowing celery's headers."""

    async def _publish(self, **options):
        prod = self.producer()
        await self.app.amqp.asend_task_message(
            prod,
            "foo",
            self.app.amqp.as_task_v2(uuid(), "foo", **options),
            retry=False,
        )
        return prod.publish.await_args.kwargs

    async def test_a_countdown_becomes_a_timestamp(self):
        eta = datetime.now(UTC) + timedelta(seconds=30)

        published = await self._publish(eta=eta)

        assert published["eta"] == eta.timestamp()

    async def test_a_message_without_an_eta_has_no_eta_property(self):
        published = await self._publish()

        assert "eta" not in published

    async def test_an_eta_that_is_not_a_time_is_not_published_as_now(self):
        prod = self.producer()
        message = self.app.amqp.as_task_v2(uuid(), "foo")
        message.headers["eta"] = "the day after tomorrow"

        with pytest.raises(ValueError):
            await self.app.amqp.asend_task_message(prod, "foo", message, retry=False)

        prod.publish.assert_not_awaited()


class test_as_task_v2(test_AMQP_Base):
    def test_raises_if_args_is_not_tuple(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), "foo", args="123")

    def test_raises_if_kwargs_is_not_mapping(self):
        with pytest.raises(TypeError):
            self.app.amqp.as_task_v2(uuid(), "foo", kwargs=(1, 2, 3))

    def test_countdown_to_eta(self):
        now = to_utc(datetime.now(UTC)).astimezone(self.app.timezone)
        m = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            countdown=10,
            now=now,
        )
        assert m.headers["eta"] == (now + timedelta(seconds=10)).isoformat()

    def test_expires_to_datetime(self):
        now = to_utc(datetime.now(UTC)).astimezone(self.app.timezone)
        m = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            expires=30,
            now=now,
        )
        assert m.headers["expires"] == (now + timedelta(seconds=30)).isoformat()

    def test_eta_to_datetime(self):
        eta = datetime.now(UTC)
        m = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            eta=eta,
        )
        assert m.headers["eta"] == eta.isoformat()

    async def test_compression(self):
        self.app.conf.task_compression = "gzip"

        prod = self.producer()
        await self.app.amqp.asend_task_message(prod, "foo", self.simple_message_no_sent_event, compression=None)
        assert prod.publish.call_args[1]["compression"] == "gzip"

    async def test_compression_override(self):
        self.app.conf.task_compression = "gzip"

        prod = self.producer()
        await self.app.amqp.asend_task_message(prod, "foo", self.simple_message_no_sent_event, compression="bz2")
        assert prod.publish.call_args[1]["compression"] == "bz2"

    def test_callbacks_errbacks_chord(self):
        @self.app.task
        def t(i):
            pass

        m = self.app.amqp.as_task_v2(
            uuid(),
            "foo",
            callbacks=[t.s(1), t.s(2)],
            errbacks=[t.s(3), t.s(4)],
            chord=t.s(5),
        )
        _, _, embed = m.body
        assert embed["callbacks"] == [utf8dict(t.s(1)), utf8dict(t.s(2))]
        assert embed["errbacks"] == [utf8dict(t.s(3)), utf8dict(t.s(4))]
        assert embed["chord"] == utf8dict(t.s(5))
