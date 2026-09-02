# Partially from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Generic process mailbox - async implementation."""

import asyncio
import socket
from collections import defaultdict, deque
from copy import copy
from itertools import count
from time import time
from typing import TYPE_CHECKING, Any

from . import Consumer, Exchange, Producer, Queue
from .clocks import LamportClock
from .common import maybe_declare, oid_from
from .exceptions import InconsistencyError
from .log import get_logger
from .matcher import match
from .utils.functional import reprcall
from .utils.objects import cached_property
from .utils.uuid import uuid

if TYPE_CHECKING:
    from collections.abc import Callable

REPLY_QUEUE_EXPIRES = 10

W_PIDBOX_IN_USE = """\
A node named {node.hostname} is already using this process mailbox!

Maybe you forgot to shutdown the other node or did your hostname change?

Ensure that only one node is using the same mailbox at any given time.
"""

__all__ = ("Mailbox", "Node")
logger = get_logger(__name__)
debug, error = logger.debug, logger.error


class Node:
    """Mailbox node."""

    #: hostname of the node.
    hostname: str

    #: the :class:`Mailbox` this is a node for.
    mailbox: Mailbox

    #: map of method name/handlers.
    handlers: dict[str, Callable[..., Any]]

    #: current context (passed on to handlers)
    state: Any = None

    #: current channel.
    channel: Any = None

    def __init__(
        self,
        hostname: str,
        state: Any = None,
        channel: Any = None,
        handlers: dict[str, Callable[..., Any]] | None = None,
        *,
        # Required: __init__ dereferences it immediately, so a Node without a
        # mailbox has never been constructible.
        mailbox: Mailbox,
    ):
        self.channel = channel
        self.mailbox = mailbox
        self.hostname = hostname
        self.state = state
        self.adjust_clock = self.mailbox.clock.adjust
        if handlers is None:
            handlers = {}
        self.handlers = handlers

    def Consumer(self, channel=None, no_ack=True, accept=None, **options):
        queue = self.mailbox.get_queue(self.hostname)

        return Consumer(
            channel or self.channel,
            [queue],
            no_ack=no_ack,
            accept=self.mailbox.accept if accept is None else accept,
            **options,
        )

    def handler(self, fun):
        self.handlers[fun.__name__] = fun
        return fun

    def on_decode_error(self, message, exc):
        error("Cannot decode message: %r", exc, exc_info=1)

    async def listen(self, channel=None, callback=None):
        consumer = self.Consumer(
            channel=channel,
            callbacks=[callback or self.handle_message],
            on_decode_error=self.on_decode_error,
        )
        # Pidbox queues are exclusive by default, so a second node claiming the
        # same hostname now gets RESOURCE_LOCKED instead of quietly sharing the
        # queue. Upstream kombu (9bece764) translates it in Node.Consumer, but
        # here nothing touches the broker until consume() runs the declare.
        conn = self.mailbox.connection
        locked_errors = conn.resource_locked_errors if conn is not None else ()
        try:
            await consumer.consume()
        except locked_errors as exc:
            raise InconsistencyError(W_PIDBOX_IN_USE.format(node=self)) from exc
        return consumer

    async def dispatch(self, method, arguments=None, reply_to=None, ticket=None, **kwargs):
        arguments = arguments or {}
        debug(
            "pidbox received method %s [reply_to:%s ticket:%s]",
            reprcall(method, (), kwargs=arguments),
            reply_to,
            ticket,
        )
        handle = (reply_to and self.handle_call) or self.handle_cast
        try:
            reply = handle(method, arguments)
            if asyncio.iscoroutine(reply):
                reply = await reply
        except SystemExit:
            raise
        except Exception as exc:
            error("pidbox command error: %r", exc, exc_info=1)
            reply = {"error": repr(exc)}

        if reply_to:
            await self.reply(
                {self.hostname: reply},
                exchange=reply_to["exchange"],
                routing_key=reply_to["routing_key"],
                ticket=ticket,
            )
        return reply

    def handle(self, method, arguments=None):
        arguments = {} if not arguments else arguments
        return self.handlers[method](self.state, **arguments)

    def handle_call(self, method, arguments):
        return self.handle(method, arguments)

    def handle_cast(self, method, arguments):
        return self.handle(method, arguments)

    async def handle_message(self, body, message=None):
        destination = body.get("destination")
        pattern = body.get("pattern")
        matcher = body.get("matcher")
        if message:
            self.adjust_clock(message.headers.get("clock") or 0)
        hostname = self.hostname
        run_dispatch = False
        if destination:
            if hostname in destination:
                run_dispatch = True
        elif pattern and matcher:
            if match(hostname, pattern, matcher):
                run_dispatch = True
        else:
            run_dispatch = True
        if run_dispatch:
            return await self.dispatch(**body)
        return None

    dispatch_from_message = handle_message

    async def reply(self, data, exchange, routing_key, ticket, **kwargs):
        kwargs.setdefault("serializer", self.mailbox.serializer)
        await self.mailbox._publish_reply(
            data,
            exchange,
            routing_key,
            ticket,
            channel=self.channel,
            **kwargs,
        )


class Mailbox:
    """Process Mailbox."""

    node_cls = Node
    exchange_fmt = "%s.pidbox"
    reply_exchange_fmt = "reply.%s.pidbox"

    #: Name of application.
    namespace: str

    #: Connection (if bound).
    connection: Any = None

    #: Exchange type (usually direct, or fanout for broadcast).
    type = "direct"

    #: exchange to send replies to.
    reply_exchange: Exchange

    #: Only accepts json messages by default.
    accept: list[str] = ["json"]

    #: Message serializer
    serializer: str | None = None

    def __init__(
        self,
        namespace: str,
        type: str = "direct",
        connection: Any = None,
        clock: LamportClock | None = None,
        accept: list[str] | None = None,
        serializer: str | None = None,
        queue_ttl: float | None = None,
        queue_expires: float | None = None,
        queue_durable: bool = False,
        queue_exclusive: bool | None = None,
        reply_queue_ttl: float | None = None,
        reply_queue_expires: float = 10.0,
    ):
        self.namespace = namespace
        self.connection = connection
        self.type = type
        self.clock = LamportClock() if clock is None else clock
        self.exchange = self._get_exchange(self.namespace, self.type)
        self.reply_exchange = self._get_reply_exchange(self.namespace)
        self.unclaimed: defaultdict[str, deque] = defaultdict(deque)
        self.accept = self.accept if accept is None else accept
        self.serializer = self.serializer if serializer is None else serializer
        self.queue_ttl = queue_ttl
        self.queue_expires = queue_expires
        self.queue_durable = queue_durable
        # RabbitMQ 4.3.0 refuses to redeclare a non-exclusive queue another
        # connection owns, which is what two nodes sharing a hostname do
        # (upstream kombu 9bece764). Keying off durability lets a caller ask
        # for a durable mailbox without tripping the ValueError below.
        if queue_exclusive is None:
            queue_exclusive = not queue_durable
        elif queue_exclusive and queue_durable:
            raise ValueError(
                "queue_exclusive and queue_durable cannot both be True "
                "(exclusive queues are automatically deleted and cannot be durable).",
            )
        self.queue_exclusive = queue_exclusive
        self.reply_queue_ttl = reply_queue_ttl
        self.reply_queue_expires = reply_queue_expires

    def __call__(self, connection):
        bound = copy(self)
        bound.connection = connection
        return bound

    def Node(self, hostname=None, state=None, channel=None, handlers=None):
        hostname = hostname or socket.gethostname()
        return self.node_cls(hostname, state, channel, handlers, mailbox=self)

    async def call(self, destination, command, kwargs=None, timeout=None, callback=None, channel=None):
        kwargs = {} if not kwargs else kwargs
        return await self._broadcast(
            command,
            kwargs,
            destination,
            reply=True,
            timeout=timeout,
            callback=callback,
            channel=channel,
        )

    async def cast(self, destination, command, kwargs=None):
        kwargs = {} if not kwargs else kwargs
        return await self._broadcast(command, kwargs, destination, reply=False)

    async def abcast(self, command, kwargs=None):
        kwargs = {} if not kwargs else kwargs
        return await self._broadcast(command, kwargs, reply=False)

    async def multi_call(self, command, kwargs=None, timeout=1, limit=None, callback=None, channel=None):
        kwargs = {} if not kwargs else kwargs
        return await self._broadcast(
            command,
            kwargs,
            reply=True,
            timeout=timeout,
            limit=limit,
            callback=callback,
            channel=channel,
        )

    def get_reply_queue(self):
        oid = self.oid
        return Queue(
            f"{oid}.{self.reply_exchange.name}",
            exchange=self.reply_exchange,
            routing_key=oid,
            durable=self.queue_durable,
            exclusive=self.queue_exclusive,
            auto_delete=not self.queue_durable,
            expires=self.reply_queue_expires,
            message_ttl=self.reply_queue_ttl,
        )

    @cached_property
    def reply_queue(self):
        return self.get_reply_queue()

    def get_queue(self, hostname):
        return Queue(
            f"{hostname}.{self.namespace}.pidbox",
            exchange=self.exchange,
            durable=self.queue_durable,
            exclusive=self.queue_exclusive,
            auto_delete=not self.queue_durable,
            expires=self.queue_expires,
            message_ttl=self.queue_ttl,
        )

    async def _publish_reply(self, reply, exchange, routing_key, ticket, channel=None, producer=None, **opts):
        channel = channel or await self.connection.default_channel()
        exchange = Exchange(exchange, type="direct", delivery_mode="transient", durable=False)
        # Declare the exchange so the channel knows the type for routing.
        await exchange.declare(channel)
        p = producer or Producer(self.connection, channel=channel, auto_declare=False)
        try:
            await p.publish(
                reply,
                exchange=exchange,
                routing_key=routing_key,
                headers={
                    "ticket": ticket,
                    "clock": self.clock.forward(),
                },
                **opts,
            )
        except InconsistencyError:
            # queue probably deleted and no one is expecting a reply.
            pass

    async def _publish(
        self,
        type,
        arguments,
        destination=None,
        reply_ticket=None,
        channel=None,
        timeout=None,
        serializer=None,
        pattern=None,
        matcher=None,
    ):
        message = {
            "method": type,
            "arguments": arguments,
            "destination": destination,
            "pattern": pattern,
            "matcher": matcher,
        }
        channel = channel or await self.connection.default_channel()
        exchange = self.exchange
        if reply_ticket:
            await maybe_declare(self.reply_queue, channel)
            message.update(
                ticket=reply_ticket,
                reply_to={"exchange": self.reply_exchange.name, "routing_key": self.oid},
            )
        serializer = serializer or self.serializer
        # Declare the exchange so the channel knows it's fanout (not direct).
        await exchange.declare(channel)
        p = Producer(self.connection, channel=channel, auto_declare=False)
        await p.publish(
            message,
            exchange=exchange.name,
            headers={"clock": self.clock.forward(), "expires": time() + timeout if timeout else 0},
            serializer=serializer,
        )

    async def _broadcast(
        self,
        command,
        arguments=None,
        destination=None,
        reply=False,
        timeout=1,
        limit=None,
        callback=None,
        channel=None,
        serializer=None,
        pattern=None,
        matcher=None,
    ):
        if destination is not None and not isinstance(destination, (list, tuple)):
            raise ValueError(f"destination must be a list/tuple not {type(destination)}")
        if (
            pattern is not None
            and not isinstance(pattern, str)
            and matcher is not None
            and not isinstance(matcher, str)
        ):
            raise ValueError(f"pattern and matcher must be strings not {type(pattern)}, {type(matcher)}")

        arguments = arguments or {}
        reply_ticket = (reply and uuid()) or None
        channel = channel or await self.connection.default_channel()

        # Set reply limit to number of destinations (if specified)
        if limit is None and destination:
            limit = (destination and len(destination)) or None

        serializer = serializer or self.serializer
        await self._publish(
            command,
            arguments,
            destination=destination,
            reply_ticket=reply_ticket,
            channel=channel,
            timeout=timeout,
            serializer=serializer,
            pattern=pattern,
            matcher=matcher,
        )

        if reply_ticket:
            return await self._collect(
                reply_ticket,
                limit=limit,
                timeout=timeout,
                callback=callback,
                channel=channel,
            )
        return None

    async def _collect(self, ticket, limit=None, timeout=1, callback=None, channel=None, accept=None):
        if accept is None:
            accept = self.accept
        channel = channel or await self.connection.default_channel()
        queue = self.reply_queue
        consumer = Consumer(self.connection, [queue], channel=channel, accept=accept, no_ack=True)
        responses = []
        unclaimed = self.unclaimed
        adjust_clock = self.clock.adjust

        try:
            return unclaimed.pop(ticket)
        except KeyError:
            pass

        def on_message(body, message):
            header = message.headers.get
            adjust_clock(header("clock") or 0)
            expires = header("expires")
            if expires and time() > expires:
                return
            this_id = header("ticket", ticket)
            if this_id == ticket:
                if callback:
                    callback(body)
                responses.append(body)
            else:
                unclaimed[this_id].append(body)

        consumer.register_callback(on_message)

        async def drain():
            for _i in (limit and range(limit)) or count():
                try:
                    await self.connection.drain_events(timeout=timeout)
                except TimeoutError:
                    break

        async with consumer:
            # `timeout` is the window replies are collected in, not a budget
            # spent per event. `drain_events` returns without raising for as
            # long as messages keep arriving, which on a channel shared with a
            # busy consumer they do, so with no `limit` there is nothing to end
            # the loop above; the caller then blocks on a reply set that is
            # already complete. The deadline has to be kept out here.
            if timeout:
                try:
                    await asyncio.wait_for(drain(), timeout)
                except TimeoutError:
                    pass
            else:
                await drain()
            return responses

    def _get_exchange(self, namespace, type):
        return Exchange(self.exchange_fmt % namespace, type=type, durable=False, delivery_mode="transient")

    def _get_reply_exchange(self, namespace):
        return Exchange(self.reply_exchange_fmt % namespace, type="direct", durable=False, delivery_mode="transient")

    @cached_property
    def oid(self):
        """This mailbox's identity, and the routing key of its reply queue.

        Cached: the identity mixes in the calling thread, so recomputing it
        would hand a caller on another thread a different reply queue from the
        one the replies are addressed to.
        """
        return oid_from(self)
