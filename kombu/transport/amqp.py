"""Pure asyncio AMQP transport using aio-pika.

This transport wraps aio-pika (a high-level AMQP 0.9.1 client library built
on aiormq) to provide native AMQP support. All exchange, queue, and binding
management is handled server-side by the broker (e.g. RabbitMQ).

Connection String
=================
.. code-block::

    amqp://[USER:PASSWORD@]HOST[:PORT][/VHOST]
    amqps://[USER:PASSWORD@]HOST[:PORT][/VHOST]

Transport Options
=================
* ``prefetch_count``: how many unacknowledged messages the broker may have
  outstanding per consumer (default: 0, unlimited). This is the only
  backpressure AMQP offers a consumer: RabbitMQ answers ``channel.flow`` with
  NOT_IMPLEMENTED, so a consumer that sets no prefetch is sent the whole queue.
* ``publisher_confirms``: Enable publisher confirms (default: True)
* ``heartbeat``: AMQP heartbeat interval in seconds
* ``connection_timeout``: seconds to wait for the connection and for the
  broker's answers to it
* ``ssl``: an :class:`ssl.SSLContext`, or a mapping of ``ca_certs``,
  ``certfile``, ``keyfile`` and ``cert_reqs`` as kombu spells them. Either one
  makes the connection TLS, whichever scheme the URL names.
"""

import asyncio
import base64
import ssl
import uuid
import weakref
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    import aio_pika
    import aio_pika.abc
    from aiormq import exceptions as aiormq_exc
else:
    try:
        import aio_pika
        import aio_pika.abc
        from aiormq import exceptions as aiormq_exc
    except ImportError:
        aio_pika = None
        aiormq_exc = None

from kombu.log import get_logger
from kombu.message import Message
from kombu.transport.base import Transport as BaseTransport
from kombu.utils.json import loads as json_loads
from kombu.utils.url import maybe_sanitize_url

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue

__all__ = ("Channel", "Transport")

logger = get_logger("kombu.transport.amqp")

# ---------------------------------------------------------------------------
# Error tuples
# ---------------------------------------------------------------------------

if aio_pika is not None:
    _amqp_connection_errors: tuple[type[Exception], ...] = (
        ConnectionError,
        ConnectionRefusedError,
        ConnectionResetError,
        TimeoutError,
        OSError,
        aiormq_exc.AMQPConnectionError,
    )
    _amqp_channel_errors: tuple[type[Exception], ...] = (
        aiormq_exc.AMQPChannelError,
        # A bare RuntimeError aio-pika raises for any operation on a channel
        # the broker took away, including reopening one whose connection is gone.
        aiormq_exc.ChannelInvalidStateError,
    )
    # aiormq raises a distinct class per AMQP reply code and, unlike py-amqp,
    # puts no `code` attribute on the exception, so 405 is matched by type.
    _amqp_resource_locked_errors: tuple[type[Exception], ...] = (aiormq_exc.ChannelLockedResource,)
else:
    _amqp_connection_errors = ()
    _amqp_channel_errors = ()
    _amqp_resource_locked_errors = ()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_exchange_type(type_name: str) -> aio_pika.ExchangeType:
    """Map kombu exchange type name to aio-pika ExchangeType."""
    return {
        "direct": aio_pika.ExchangeType.DIRECT,
        "fanout": aio_pika.ExchangeType.FANOUT,
        "topic": aio_pika.ExchangeType.TOPIC,
        "headers": aio_pika.ExchangeType.HEADERS,
    }.get(type_name, aio_pika.ExchangeType.DIRECT)


def _as_connection_error(exc: BaseException | None) -> Exception:
    """Return the close reason as something :attr:`Transport.connection_errors` covers.

    aio-pika reports a close with whatever ended the connection, from an
    aiormq ``ConnectionClosed`` down to a bare ``CancelledError`` or nothing
    at all, and a caller that is about to reconnect has to be able to catch it.
    """
    if isinstance(exc, _amqp_connection_errors):
        return exc
    return aiormq_exc.ConnectionClosed(exc if exc is not None else "connection closed")


#: kombu's TLS option names mapped onto the query parameters aiormq reads.
_SSL_QUERY_PARAMETERS = {
    "ca_certs": "cafile",
    "cafile": "cafile",
    "capath": "capath",
    "cadata": "cadata",
    "certfile": "certfile",
    "keyfile": "keyfile",
}


def _ssl_query(options: Mapping[str, Any]) -> dict[str, str]:
    """Translate kombu's ``ssl`` mapping into URL query parameters."""
    query = {
        _SSL_QUERY_PARAMETERS[name]: str(value)
        for name, value in options.items()
        if name in _SSL_QUERY_PARAMETERS and value is not None
    }
    if options.get("cert_reqs") == ssl.CERT_NONE:
        query["no_verify_ssl"] = "1"
    return query


#: How many messages to buffer for a consumer that set no prefetch count.
_UNTHROTTLED_BUFFER_SIZE = 1000

#: Content encodings that name a byte stream rather than a Python text codec.
_BINARY_CONTENT_ENCODINGS = frozenset({"binary", "ascii-8bit"})


def _envelope_body_to_bytes(
    body: Any,
    content_encoding: str,
    headers: dict[str, Any],
) -> bytes:
    """Return the bytes an envelope body puts on the wire.

    AMQP bodies are bytes, so a binary serializer's payload travels exactly as
    it was produced. The producer base64-wraps such a payload only to fit it
    into the JSON envelope and records that with a ``body_encoding`` header;
    the header describes the envelope, not the AMQP message, so it is consumed
    here rather than published.
    """
    if isinstance(body, bytes):
        return body
    if not isinstance(body, str):
        body = str(body)
    if headers.get("body_encoding") == "base64":
        del headers["body_encoding"]
        return base64.b64decode(body)
    if content_encoding in _BINARY_CONTENT_ENCODINGS:
        return body.encode("utf-8")
    return body.encode(content_encoding)


def _expiration_to_millis(expiration: float | timedelta | datetime) -> int:
    """Normalise aio-pika's expiration to the AMQP wire format (milliseconds).

    Incoming messages carry a float in seconds (aio-pika decodes the header for
    us), but the same attribute holds a timedelta or an absolute datetime on
    messages we built ourselves, so every form has to be handled.
    """
    if isinstance(expiration, timedelta):
        return int(expiration.total_seconds() * 1000)
    if isinstance(expiration, datetime):
        return int((expiration - datetime.now(UTC)).total_seconds() * 1000)
    return int(expiration * 1000)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class Channel:
    """AMQP channel wrapping an aio-pika Channel.

    Bridges aio-pika's callback-based consume model to kombu's
    drain_events pull model using an asyncio.Queue buffer.
    """

    def __init__(
        self,
        aio_channel: aio_pika.abc.AbstractChannel,
        transport: Transport | None = None,
        prefetch_count: int = 0,
    ) -> None:
        # The transport is what a channel goes back to for a new aio-pika
        # channel after the connection it was opened on has gone.
        self._transport = transport
        self._closed = False

        # Consumer state: tag -> (queue_name, callback, no_ack)
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self.no_ack_consumers: set[str] | None = set()

        # Declared aio-pika objects (cached for reuse)
        self._declared_exchanges: dict[str, aio_pika.abc.AbstractExchange] = {}
        self._declared_queues: dict[str, aio_pika.abc.AbstractQueue] = {}

        # What it takes to put the queues and their bindings back on a channel
        # the broker closed, or on a new channel after a connection loss.
        self._queue_declarations: dict[str, dict[str, Any]] = {}
        self._bindings: list[dict[str, Any]] = []

        # The prefetch window bounds the incoming buffer; without one aiormq
        # runs a task per delivery, so the bound below is all that limits a deep queue.
        self._prefetch_count = prefetch_count
        self._message_queue: asyncio.Queue[tuple[str, Message]] = asyncio.Queue(
            maxsize=prefetch_count or _UNTHROTTLED_BUFFER_SIZE,
        )

        # delivery_tag bridging: str(amqp_int_tag) -> aio-pika IncomingMessage
        self._delivery_tag_map: dict[str, aio_pika.abc.AbstractIncomingMessage] = {}

        # Set when the broker takes the channel or connection away, so a parked
        # drain_events wakes up instead of waiting for a message that cannot arrive.
        self._interrupted = asyncio.Event()
        self._connection_error: Exception | None = None
        self._lost_connection = False

        self._aio_channel: aio_pika.abc.AbstractChannel
        self._attach(aio_channel)

    # ---- recovery ----------------------------------------------------------

    def _attach(self, aio_channel: aio_pika.abc.AbstractChannel) -> None:
        self._aio_channel = aio_channel
        aio_channel.close_callbacks.add(self._on_aio_channel_closed)

    def _on_aio_channel_closed(
        self,
        _sender: Any,
        exc: BaseException | None = None,
    ) -> None:
        """aio-pika callback: the broker closed this channel."""
        if self._closed:
            return
        logger.warning("AMQP channel closed by the broker: %r", exc)
        self._interrupted.set()

    def on_connection_closed(self, exc: BaseException | None = None) -> None:
        """Record that the connection this channel was opened on is gone.

        Called by the transport. The loss is reported to the next caller, once,
        so it can rebuild whatever state it keeps alongside the channel; the
        channel itself then moves to the connection that replaced the lost one.
        """
        if self._closed or self._lost_connection:
            return
        self._lost_connection = True
        self._connection_error = _as_connection_error(exc)
        self._interrupted.set()

    def _raise_if_connection_lost(self) -> None:
        """Report a lost connection to one caller and then stop reporting it."""
        if self._connection_error is not None:
            exc, self._connection_error = self._connection_error, None
            raise exc

    async def _ensure_open(self) -> None:
        """Make the channel usable again after the broker or the network cut it.

        A closed channel takes its consumers and its unacknowledged deliveries
        with it, and a lost connection takes the channel itself, so both are
        rebuilt here before the caller's operation runs.
        """
        if self._closed:
            return

        self._raise_if_connection_lost()

        if self._lost_connection:
            await self._reattach()
        elif self._aio_channel.is_closed:
            logger.info("Reopening AMQP channel")
            try:
                await self._aio_channel.reopen()
            except aiormq_exc.ChannelInvalidStateError as exc:
                # aio-pika refuses to reopen a channel whose connection has no
                # transport any more, which means the connection went too.
                self.on_connection_closed(exc)
                self._raise_if_connection_lost()
                raise
            await self._restore()

    async def _reattach(self) -> None:
        """Open a channel on the connection that replaced the lost one."""
        if self._transport is None:
            raise _as_connection_error(None)

        logger.info("Moving AMQP channel to a new connection")
        self._attach(await self._transport.new_aio_channel())
        self._lost_connection = False
        self._connection_error = None
        # The cached exchanges publish through a channel that no longer
        # exists, and looking one up again costs nothing on the wire.
        self._declared_exchanges.clear()
        await self._restore()

    async def _restore(self) -> None:
        """Declare again what the closed channel took with it."""
        self._drop_buffered_redeliveries()
        self._delivery_tag_map.clear()
        self._interrupted.clear()

        for name in list(self._declared_queues):
            self._declared_queues[name] = await self._resolve_queue(name)
        for binding in list(self._bindings):
            await self._bind(**binding)
        for tag, (queue_name, _callback, no_ack) in list(self._consumers.items()):
            await self._start_aio_consumer(tag, queue_name, no_ack)

    def _drop_buffered_redeliveries(self) -> None:
        """Forget the buffered messages the broker is about to send again."""
        buffered = []
        while True:
            try:
                buffered.append(self._message_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        for item in buffered:
            # The broker requeues unacked deliveries on close, so keeping this
            # copy would run it twice; a no-ack delivery is not coming back, so it stays.
            if item[1].delivery_tag not in self._delivery_tag_map:
                self._message_queue.put_nowait(item)

    # ---- close -------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if not self._lost_connection and not self._aio_channel.is_closed:
            try:
                for tag in list(self._consumers):
                    await self.basic_cancel(tag)
                await self._aio_channel.close()
            except (*_amqp_connection_errors, *_amqp_channel_errors) as exc:
                # The broker took the channel away while we were handing it
                # back, which leaves nothing left to cancel or close.
                logger.debug("AMQP channel was already gone when closing it: %r", exc)

        self._consumers.clear()
        self._delivery_tag_map.clear()

    # ---- exchange operations -----------------------------------------------

    async def declare_exchange(self, exchange: Exchange) -> None:
        if not exchange.name or exchange.name in self._declared_exchanges:
            return
        await self._ensure_open()

        aio_exchange = await self._aio_channel.declare_exchange(
            name=exchange.name,
            type=_get_exchange_type(exchange.type),
            durable=exchange.durable,
            auto_delete=exchange.auto_delete,
            arguments=exchange.arguments or None,
        )
        self._declared_exchanges[exchange.name] = aio_exchange

    async def exchange_delete(self, exchange: str) -> None:
        await self._ensure_open()
        await self._aio_channel.exchange_delete(exchange)
        self._declared_exchanges.pop(exchange, None)
        self._bindings = [b for b in self._bindings if b["exchange"] != exchange]

    async def _get_exchange(self, exchange: str) -> aio_pika.abc.AbstractExchange:
        """Return the cached exchange object, or a handle to an existing one."""
        aio_exchange = self._declared_exchanges.get(exchange)
        if aio_exchange is None:
            aio_exchange = await self._aio_channel.get_exchange(exchange, ensure=False)
            self._declared_exchanges[exchange] = aio_exchange
        return aio_exchange

    # ---- queue operations --------------------------------------------------

    async def declare_queue(self, queue: Queue) -> str:
        await self._ensure_open()
        arguments = {}
        if hasattr(queue, "queue_arguments") and queue.queue_arguments:
            arguments.update(queue.queue_arguments)

        declaration: dict[str, Any] = {
            "name": queue.name or None,  # None -> server-generated name
            "durable": queue.durable,
            "exclusive": queue.exclusive,
            "auto_delete": queue.auto_delete,
            "arguments": arguments or None,
        }
        aio_queue = await self._aio_channel.declare_queue(**declaration)

        actual_name = aio_queue.name
        queue.name = actual_name
        self._declared_queues[actual_name] = aio_queue
        # Pin a server-generated name so a redeclare after a close asks for the
        # queue the caller is already bound to rather than a second one.
        declaration["name"] = actual_name
        self._queue_declarations[actual_name] = declaration
        return actual_name

    async def queue_bind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        if not exchange:
            return  # Default exchange: bindings are implicit in AMQP

        await self._ensure_open()
        binding: dict[str, Any] = {
            "queue": queue,
            "exchange": exchange,
            "routing_key": routing_key,
            "arguments": arguments,
        }
        await self._bind(**binding)
        if binding not in self._bindings:
            self._bindings.append(binding)

    async def _bind(
        self,
        queue: str,
        exchange: str,
        routing_key: str,
        arguments: dict | None,
    ) -> None:
        aio_queue = await self._queue(queue)
        aio_exchange = await self._get_exchange(exchange)
        await aio_queue.bind(aio_exchange, routing_key=routing_key, arguments=arguments)

    async def _queue(self, queue: str) -> aio_pika.abc.AbstractQueue:
        """Return the cached queue object, or a handle to an existing one."""
        aio_queue = self._declared_queues.get(queue)
        if aio_queue is None:
            aio_queue = await self._resolve_queue(queue)
            self._declared_queues[queue] = aio_queue
        return aio_queue

    async def _resolve_queue(self, queue: str) -> aio_pika.abc.AbstractQueue:
        """Return a handle to a queue on this channel.

        A queue this channel declared itself is declared again, so that a
        non-durable one the broker dropped comes back. Any other queue is only
        addressed; the broker answers a name that does not exist with 404 when
        the operation reaches it.
        """
        declaration = self._queue_declarations.get(queue)
        if declaration is not None:
            return await self._aio_channel.declare_queue(**declaration)
        return await self._aio_channel.get_queue(queue, ensure=False)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        await self._ensure_open()
        aio_queue = await self._queue(queue)
        aio_exchange = await self._get_exchange(exchange)
        await aio_queue.unbind(aio_exchange, routing_key=routing_key, arguments=arguments)

        binding = {
            "queue": queue,
            "exchange": exchange,
            "routing_key": routing_key,
            "arguments": arguments,
        }
        if binding in self._bindings:
            self._bindings.remove(binding)

    async def queue_purge(self, queue: str) -> int:
        await self._ensure_open()
        result = await (await self._queue(queue)).purge()
        return getattr(result, "message_count", 0)

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        await self._ensure_open()
        result = await self._aio_channel.queue_delete(
            queue,
            if_unused=if_unused,
            if_empty=if_empty,
        )
        self._declared_queues.pop(queue, None)
        self._queue_declarations.pop(queue, None)
        self._bindings = [b for b in self._bindings if b["queue"] != queue]
        return getattr(result, "message_count", 0)

    # ---- publish -----------------------------------------------------------

    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        await self._ensure_open()
        envelope = json_loads(message)

        content_type = envelope.get("content-type", "application/json")
        content_encoding = envelope.get("content-encoding", "utf-8")
        properties = envelope.get("properties", {})
        headers = envelope.get("headers", {})

        body_bytes = _envelope_body_to_bytes(envelope.get("body", ""), content_encoding, headers)

        # Build aio-pika Message
        msg_kwargs: dict[str, Any] = {
            "body": body_bytes,
            "content_type": content_type,
            "content_encoding": content_encoding,
            "headers": headers or None,
        }

        if "priority" in properties:
            msg_kwargs["priority"] = int(properties["priority"])
        if "delivery_mode" in properties:
            msg_kwargs["delivery_mode"] = aio_pika.DeliveryMode(int(properties["delivery_mode"]))
        if "expiration" in properties:
            msg_kwargs["expiration"] = timedelta(milliseconds=int(properties["expiration"]))
        if "correlation_id" in properties:
            msg_kwargs["correlation_id"] = properties["correlation_id"]
        if "reply_to" in properties:
            msg_kwargs["reply_to"] = properties["reply_to"]
        if "message_id" in properties:
            msg_kwargs["message_id"] = properties["message_id"]
        if "timestamp" in properties:
            msg_kwargs["timestamp"] = datetime.fromtimestamp(
                float(properties["timestamp"]),
                tz=UTC,
            )
        if "app_id" in properties:
            msg_kwargs["app_id"] = properties["app_id"]
        if "type" in properties:
            msg_kwargs["type"] = properties["type"]

        aio_message = aio_pika.Message(**msg_kwargs)

        if exchange:
            aio_exchange = await self._get_exchange(exchange)
        else:
            aio_exchange = self._aio_channel.default_exchange

        # AMQP (and py-amqp before us) defaults `mandatory` to off, aio-pika
        # defaults it to on; a returned message surfaces through aio-pika's
        # return callbacks either way.
        await aio_exchange.publish(
            aio_message,
            routing_key=routing_key,
            mandatory=kwargs.get("mandatory", False),
        )

    # ---- get (synchronous single fetch) ------------------------------------

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        await self._ensure_open()
        aio_queue = await self._queue(queue)

        # fail=False makes aio-pika answer an empty queue with None rather
        # than raising; every other error is the caller's to see.
        incoming = await aio_queue.get(no_ack=no_ack, fail=False)

        if incoming is None:
            return None

        delivery_tag = str(incoming.delivery_tag)
        if not no_ack:
            self._delivery_tag_map[delivery_tag] = incoming

        return self._convert_message(incoming, queue, delivery_tag, accept=accept)

    # ---- consumer operations -----------------------------------------------

    async def basic_consume(
        self,
        queue: str,
        callback: Callable,
        consumer_tag: str | None = None,
        no_ack: bool = False,
    ) -> str:
        await self._ensure_open()
        if consumer_tag is None:
            consumer_tag = str(uuid.uuid4())

        self._consumers[consumer_tag] = (queue, callback, no_ack)

        if no_ack and self.no_ack_consumers is not None:
            self.no_ack_consumers.add(consumer_tag)

        await self._start_aio_consumer(consumer_tag, queue, no_ack)
        return consumer_tag

    async def _start_aio_consumer(self, consumer_tag: str, queue: str, no_ack: bool) -> None:
        """Attach the buffering callback drain_events pulls from."""
        aio_queue = await self._queue(queue)

        async def _on_incoming(incoming: aio_pika.abc.AbstractIncomingMessage) -> None:
            tag = str(incoming.delivery_tag)
            try:
                if not no_ack:
                    self._delivery_tag_map[tag] = incoming
                msg = self._convert_message(incoming, queue, tag, consumer_tag=consumer_tag)
                await self._message_queue.put((queue, msg))
            except Exception:
                # aiormq runs this in a task per delivery and drops the result,
                # so an error raised here reaches nobody. Hand it back to the broker.
                logger.exception("Failed to accept delivery %s on queue %s", tag, queue)
                self._delivery_tag_map.pop(tag, None)
                if not no_ack:
                    await incoming.nack(requeue=True)

        await aio_queue.consume(
            callback=_on_incoming,
            no_ack=no_ack,
            consumer_tag=consumer_tag,
        )

    async def basic_cancel(self, consumer_tag: str) -> None:
        entry = self._consumers.pop(consumer_tag, None)
        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)
        if entry is None:
            return

        aio_queue = self._declared_queues.get(entry[0])
        # A channel the broker has taken away carries no consumers any more,
        # and cancelling on it would only raise.
        if aio_queue is not None and not self._lost_connection and not self._aio_channel.is_closed:
            await aio_queue.cancel(consumer_tag)

    # ---- drain_events ------------------------------------------------------

    async def drain_events(self, timeout: float | None = None) -> bool:
        """Deliver one buffered message to its consumer.

        Returns False when nothing arrived before ``timeout``. Raises a member
        of :attr:`Transport.connection_errors` once the connection is gone, so
        the caller can reconnect instead of waiting on a buffer nothing will
        ever fill again. A channel the broker closed on its own is reopened
        here, with its queues and consumers restored.
        """
        await self._ensure_open()

        if timeout is not None and timeout <= 0:
            # A non-blocking poll. asyncio.wait_for(timeout=0) cancels the get
            # before it runs and reports a timeout even with messages buffered.
            try:
                queue_name, message = self._message_queue.get_nowait()
            except asyncio.QueueEmpty:
                return False
        else:
            getter = asyncio.ensure_future(self._message_queue.get())
            interrupted = asyncio.ensure_future(self._interrupted.wait())
            try:
                await asyncio.wait(
                    (getter, interrupted),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                interrupted.cancel()

            if not getter.done():
                getter.cancel()
                self._raise_if_connection_lost()
                return False
            queue_name, message = getter.result()

        await self._deliver_to_consumer(queue_name, message)
        return True

    async def _deliver_to_consumer(self, queue: str, message: Message) -> None:
        """Hand a message to the consumer the broker delivered it to."""
        callback = self._callback_for(queue, message.delivery_info.get("consumer_tag", ""))
        if callback is None:
            # The consumer was cancelled between the delivery and this drain.
            # Hand the message back rather than dropping it.
            logger.warning(
                "No consumer left on queue %s for delivery %s, requeueing it",
                queue,
                message.delivery_tag,
            )
            if message.delivery_tag is not None:
                await self.basic_reject(message.delivery_tag, requeue=True)
            return

        try:
            body = message.decode()
        except Exception:
            # The callback still gets the message, but a payload that cannot
            # be read must not pass for a normal one without a word.
            logger.exception(
                "Failed to decode message %s from queue %s, delivering it undecoded",
                message.delivery_tag,
                queue,
            )
            body = message.body

        result = callback(body, message)
        if asyncio.iscoroutine(result):
            await result

    def _callback_for(self, queue: str, consumer_tag: str) -> Callable | None:
        """Return the callback registered for a delivery.

        Several consumers can share a queue with a callback each, so the
        consumer tag the broker sent the message to picks the one that asked
        for it. A message that carries no tag we know falls back to whoever
        consumes that queue.
        """
        entry = self._consumers.get(consumer_tag)
        if entry is not None:
            return entry[1]
        for registered_queue, callback, _no_ack in self._consumers.values():
            if registered_queue == queue:
                return callback
        return None

    # ---- ack / reject / recover -------------------------------------------

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        incoming = self._delivery_tag_map.pop(delivery_tag, None)
        if incoming is None:
            return
        if multiple:
            self._forget_tags_up_to(delivery_tag)
        await incoming.ack(multiple=multiple)

    def _forget_tags_up_to(self, delivery_tag: str) -> None:
        """Drop every tracked tag the broker considers acknowledged.

        A multiple ack covers every delivery tag up to and including the one
        it names. Keeping the lower tags would let a later ack send a tag the
        broker has already forgotten, which it answers with PRECONDITION_FAILED
        and a closed channel.
        """
        acknowledged = int(delivery_tag)
        for tag in [t for t in self._delivery_tag_map if int(t) <= acknowledged]:
            del self._delivery_tag_map[tag]

    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        incoming = self._delivery_tag_map.pop(delivery_tag, None)
        if incoming:
            await incoming.reject(requeue=requeue)

    async def basic_qos(self, prefetch_count: int = 0) -> None:
        """Set Quality of Service (prefetch count) on the channel.

        Args:
            prefetch_count: Number of unacknowledged messages the broker
                will deliver before waiting. 0 means unlimited.

        RabbitMQ fixes a consumer's credit when the consumer is registered
        and leaves it alone afterwards, with either value of the global flag,
        so the consumers already running are registered again here. Without
        that a prefetch set after consuming started would change nothing.
        """
        await self._ensure_open()
        await self._aio_channel.set_qos(prefetch_count=prefetch_count)
        if prefetch_count == self._prefetch_count:
            return

        self._prefetch_count = prefetch_count
        for tag, (queue_name, _callback, no_ack) in list(self._consumers.items()):
            aio_queue = self._declared_queues.get(queue_name)
            if aio_queue is not None:
                await aio_queue.cancel(tag)
            await self._start_aio_consumer(tag, queue_name, no_ack)

    async def basic_recover(self, requeue: bool = True) -> None:
        """Request the broker to redeliver all unacknowledged messages.

        aio-pika has no wrapper for basic.recover, so the frame goes out
        through the aiormq channel underneath.

        Args:
            requeue: If True (default), the broker requeues messages so
                they may be delivered to other consumers. If False, they
                are redelivered to this consumer.
        """
        await self._ensure_open()
        underlay = await self._aio_channel.get_underlay_channel()
        await underlay.basic_recover(requeue=requeue)

    # ---- message conversion ------------------------------------------------

    def _convert_message(
        self,
        incoming: aio_pika.abc.AbstractIncomingMessage,
        queue: str,
        delivery_tag: str,
        consumer_tag: str = "",
        accept: AbstractSet[str] | None = None,
    ) -> Message:
        """Convert an aio-pika IncomingMessage to a kombu Message."""
        properties: dict[str, Any] = {"delivery_tag": delivery_tag}

        if incoming.priority is not None:
            properties["priority"] = incoming.priority
        if incoming.delivery_mode is not None:
            properties["delivery_mode"] = incoming.delivery_mode.value
        if incoming.expiration is not None:
            properties["expiration"] = str(_expiration_to_millis(incoming.expiration))
        if incoming.correlation_id:
            properties["correlation_id"] = incoming.correlation_id
        if incoming.reply_to:
            properties["reply_to"] = incoming.reply_to
        if incoming.message_id:
            properties["message_id"] = incoming.message_id
        if incoming.timestamp:
            properties["timestamp"] = incoming.timestamp.timestamp()
        if incoming.app_id:
            properties["app_id"] = incoming.app_id
        if incoming.type:
            properties["type"] = incoming.type

        headers = dict(incoming.headers) if incoming.headers else {}
        body = incoming.body
        if headers.get("body_encoding") == "base64":
            del headers["body_encoding"]
            body = base64.b64decode(body)

        return Message(
            body=body,
            delivery_tag=delivery_tag,
            content_type=incoming.content_type or "application/octet-stream",
            content_encoding=incoming.content_encoding or "utf-8",
            delivery_info={
                "exchange": incoming.exchange or "",
                "routing_key": incoming.routing_key or "",
                "delivery_tag": delivery_tag,
                "consumer_tag": consumer_tag,
                "redelivered": getattr(incoming, "redelivered", False),
            },
            properties=properties,
            headers=headers,
            accept=accept,
            channel=self,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )

    # ---- context manager ---------------------------------------------------

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


_Channel = Channel


class Transport(BaseTransport):
    """AMQP transport using aio-pika.

    Wraps aio-pika to provide native AMQP 0.9.1 support.
    Exchange, queue, and binding management is done server-side
    by the broker (e.g. RabbitMQ).
    """

    Channel = _Channel  # type: ignore[assignment]
    default_port = 5672

    driver_type = "amqp"
    driver_name = "aio-pika"

    exchange_types = {"direct", "fanout", "topic", "headers"}

    connection_errors = tuple(
        set(
            BaseTransport.connection_errors + (ConnectionRefusedError, TimeoutError) + _amqp_connection_errors,
        ),
    )

    channel_errors = BaseTransport.channel_errors + _amqp_channel_errors

    resource_locked_errors = _amqp_resource_locked_errors

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        **options: Any,
    ) -> None:
        if aio_pika is None:
            raise ImportError(
                "aio-pika package is required for AMQP transport. Install it with: pip install 'celery-asyncio[amqp]'",
            )
        super().__init__(url, **options)
        self._connection: aio_pika.abc.AbstractConnection | None = None
        # Weak: a channel the caller has dropped must not be kept alive just
        # so close() can walk it.
        self._channels: weakref.WeakSet[Channel] = weakref.WeakSet()

    async def connect(self) -> None:
        """Open a connection, or replace one the broker has taken away.

        aio-pika only marks a connection closed when the close came from this
        side, so a broker restart or a server-side close leaves an object that
        answers every request with "closed". Reconnecting here is what lets
        ``Connection.ensure_connection`` mean anything on this transport.
        """
        if self.is_connected:
            return

        if self._connection is not None:
            self._discard_connection()

        url, kwargs = self._connect_arguments()
        connection = await aio_pika.connect(url, **kwargs)
        self._connection = connection
        connection.close_callbacks.add(self._on_connection_closed)
        logger.debug("Connected to AMQP broker at %s", maybe_sanitize_url(self._url))

    def _connect_arguments(self) -> tuple[str, dict[str, Any]]:
        """Return the URL and the keyword arguments to connect with.

        aio-pika builds a URL out of its keyword arguments only when it is not
        given one, and ignores them otherwise, while aiormq reads the
        heartbeat, the timeout and the TLS files from the URL query alone. An
        option that arrives as a transport option therefore has to be folded
        into the URL; passing it alongside would drop it without a word.
        """
        url = urlsplit(self._url)
        query = dict(parse_qsl(url.query))
        scheme = url.scheme
        kwargs: dict[str, Any] = {}

        if "heartbeat" in self._options:
            query["heartbeat"] = str(self._options["heartbeat"])
        if "connection_timeout" in self._options:
            timeout = self._options["connection_timeout"]
            query["timeout"] = str(timeout)
            kwargs["timeout"] = float(timeout)

        ssl_options = self._options.get("ssl")
        if ssl_options:
            scheme = "amqps"
            if isinstance(ssl_options, ssl.SSLContext):
                kwargs["ssl_context"] = ssl_options
            else:
                query.update(_ssl_query(ssl_options))

        return urlunsplit(url._replace(scheme=scheme, query=urlencode(query))), kwargs

    def _on_connection_closed(self, connection: Any, exc: BaseException | None = None) -> None:
        """aio-pika callback: this connection is gone."""
        if connection is not self._connection:
            return  # left over from a connection this transport has replaced
        logger.warning(
            "AMQP connection to %s closed: %r",
            maybe_sanitize_url(self._url),
            exc,
        )
        for channel in list(self._channels):
            channel.on_connection_closed(exc)

    def _discard_connection(self) -> None:
        """Drop a connection that is no longer usable.

        The channels are kept: each one reports the loss to its next caller
        and then moves itself to the connection that replaces this one.
        """
        for channel in list(self._channels):
            channel.on_connection_closed(None)
        self._connection = None

    async def close(self) -> None:
        for channel in list(self._channels):
            await channel.close()
        self._channels.clear()

        connection, self._connection = self._connection, None
        if connection is not None and not connection.is_closed:
            await connection.close()

    async def new_aio_channel(self) -> aio_pika.abc.AbstractChannel:
        """Open an aio-pika channel with the transport's options applied."""
        await self.connect()

        publisher_confirms = self._options.get("publisher_confirms", True)
        aio_channel = await self._connection.channel(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
            publisher_confirms=publisher_confirms,
        )

        prefetch_count = self._options.get("prefetch_count", 0)
        if prefetch_count:
            await aio_channel.set_qos(prefetch_count=prefetch_count)
        return aio_channel

    async def create_channel(self) -> _Channel:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        channel = Channel(
            await self.new_aio_channel(),
            transport=self,
            prefetch_count=self._options.get("prefetch_count", 0),
        )
        self._channels.add(channel)
        return channel

    @property
    def is_connected(self) -> bool:
        connection = self._connection
        return (
            connection is not None
            # is_closed only covers a close this side asked for; the event is
            # what aio-pika clears when the peer or the network ends it.
            and not connection.is_closed
            and connection.connected.is_set()
        )

    def driver_version(self) -> str:
        try:
            return aio_pika.__version__
        except AttributeError:
            return "N/A"
