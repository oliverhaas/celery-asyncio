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
* ``prefetch_count``: QoS prefetch count (default: 0 = unlimited)
* ``publisher_confirms``: Enable publisher confirms (default: True)
* ``heartbeat``: AMQP heartbeat interval in seconds
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Callable
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
    _amqp_channel_errors: tuple[type[Exception], ...] = (aiormq_exc.AMQPChannelError,)
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


def _expiration_to_millis(expiration: float | timedelta | datetime) -> int:
    """Normalise aio-pika's expiration to the AMQP wire format (milliseconds).

    Incoming messages carry a float in seconds (aio-pika decodes the header for
    us), but the same attribute holds a timedelta or an absolute datetime on
    messages we built ourselves, so all four forms have to be handled.
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

    def __init__(self, aio_channel: aio_pika.abc.AbstractChannel) -> None:
        self._aio_channel = aio_channel
        self._closed = False

        # Consumer state: tag -> (queue_name, callback, no_ack)
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self.no_ack_consumers: set[str] | None = set()

        # Declared aio-pika objects (cached for reuse)
        self._declared_exchanges: dict[str, aio_pika.abc.AbstractExchange] = {}
        self._declared_queues: dict[str, aio_pika.abc.AbstractQueue] = {}

        # Incoming message buffer for drain_events.
        # Bounded to prevent unbounded memory growth when the consumer
        # is slower than the broker. aio-pika will apply TCP backpressure
        # when the queue is full.
        self._message_queue: asyncio.Queue[tuple[str, Message]] = asyncio.Queue(maxsize=1000)

        # delivery_tag bridging: str(amqp_int_tag) -> aio-pika IncomingMessage
        self._delivery_tag_map: dict[str, aio_pika.abc.AbstractIncomingMessage] = {}

    # ---- close -------------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Cancel all aio-pika consumers
        for tag in list(self._consumers):
            try:
                await self.basic_cancel(tag)
            except Exception as exc:
                logger.debug("Error cancelling consumer %s: %s", tag, exc)

        self._consumers.clear()
        self._delivery_tag_map.clear()

        if not self._aio_channel.is_closed:
            try:
                await self._aio_channel.close()
            except Exception as exc:
                logger.debug("Error closing AMQP channel: %s", exc)

    # ---- exchange operations -----------------------------------------------

    async def declare_exchange(self, exchange: Exchange) -> None:
        if not exchange.name or exchange.name in self._declared_exchanges:
            return

        aio_exchange = await self._aio_channel.declare_exchange(
            name=exchange.name,
            type=_get_exchange_type(exchange.type),
            durable=exchange.durable,
            auto_delete=exchange.auto_delete,
            arguments=exchange.arguments or None,
        )
        self._declared_exchanges[exchange.name] = aio_exchange

    async def exchange_delete(self, exchange: str) -> None:
        await self._aio_channel.exchange_delete(exchange)
        self._declared_exchanges.pop(exchange, None)

    # ---- queue operations --------------------------------------------------

    async def declare_queue(self, queue: Queue) -> str:
        arguments = {}
        if hasattr(queue, "queue_arguments") and queue.queue_arguments:
            arguments.update(queue.queue_arguments)

        aio_queue = await self._aio_channel.declare_queue(
            name=queue.name or None,  # None -> server-generated name
            durable=queue.durable,
            exclusive=queue.exclusive,
            auto_delete=queue.auto_delete,
            arguments=arguments or None,
        )

        actual_name = aio_queue.name
        queue.name = actual_name
        self._declared_queues[actual_name] = aio_queue
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

        aio_queue = self._declared_queues.get(queue)
        if not aio_queue:
            return

        aio_exchange = self._declared_exchanges.get(exchange)
        if not aio_exchange:
            aio_exchange = await self._aio_channel.get_exchange(exchange, ensure=False)
            self._declared_exchanges[exchange] = aio_exchange

        await aio_queue.bind(aio_exchange, routing_key=routing_key, arguments=arguments)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        aio_queue = self._declared_queues.get(queue)
        aio_exchange = self._declared_exchanges.get(exchange)
        if aio_queue and aio_exchange:
            await aio_queue.unbind(aio_exchange, routing_key=routing_key, arguments=arguments)

    async def queue_purge(self, queue: str) -> int:
        aio_queue = self._declared_queues.get(queue)
        if aio_queue:
            result = await aio_queue.purge()
            return getattr(result, "message_count", 0)
        return 0

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        result = await self._aio_channel.queue_delete(
            queue,
            if_unused=if_unused,
            if_empty=if_empty,
        )
        self._declared_queues.pop(queue, None)
        return getattr(result, "message_count", 0)

    # ---- publish -----------------------------------------------------------

    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        # Parse kombu JSON envelope
        envelope = json_loads(message)

        body_str = envelope.get("body", "")
        content_type = envelope.get("content-type", "application/json")
        content_encoding = envelope.get("content-encoding", "utf-8")
        properties = envelope.get("properties", {})
        headers = envelope.get("headers", {})

        # Encode body back to bytes
        if isinstance(body_str, str):
            body_bytes = body_str.encode(content_encoding)
        elif isinstance(body_str, bytes):
            body_bytes = body_str
        else:
            body_bytes = str(body_str).encode(content_encoding)

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

        # Resolve exchange
        if exchange:
            aio_exchange = self._declared_exchanges.get(exchange)
            if not aio_exchange:
                aio_exchange = await self._aio_channel.get_exchange(exchange, ensure=False)
                self._declared_exchanges[exchange] = aio_exchange
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
        aio_queue = self._declared_queues.get(queue)
        if not aio_queue:
            return None

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
        if consumer_tag is None:
            consumer_tag = str(uuid.uuid4())

        self._consumers[consumer_tag] = (queue, callback, no_ack)

        if no_ack and self.no_ack_consumers is not None:
            self.no_ack_consumers.add(consumer_tag)

        # Start aio-pika consumer with internal callback that buffers
        # messages into _message_queue for drain_events to pull.
        aio_queue = self._declared_queues.get(queue)
        if aio_queue:
            queue_name = queue  # capture for closure
            _no_ack = no_ack

            _consumer_tag = consumer_tag  # capture for closure

            async def _on_incoming(incoming: aio_pika.abc.AbstractIncomingMessage) -> None:
                tag = str(incoming.delivery_tag)
                if not _no_ack:
                    self._delivery_tag_map[tag] = incoming
                msg = self._convert_message(incoming, queue_name, tag, consumer_tag=_consumer_tag)
                await self._message_queue.put((queue_name, msg))

            await aio_queue.consume(
                callback=_on_incoming,
                no_ack=no_ack,
                consumer_tag=consumer_tag,
            )

        return consumer_tag

    async def basic_cancel(self, consumer_tag: str) -> None:
        entry = self._consumers.pop(consumer_tag, None)
        if entry:
            queue_name = entry[0]
            aio_queue = self._declared_queues.get(queue_name)
            if aio_queue:
                try:
                    await aio_queue.cancel(consumer_tag)
                except Exception as exc:
                    logger.debug("Error cancelling consumer %s on queue: %s", consumer_tag, exc)

        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)

    # ---- drain_events ------------------------------------------------------

    async def drain_events(self, timeout: float | None = None) -> bool:
        if not self._consumers:
            await asyncio.sleep(0.1)
            return False

        try:
            queue_name, message = await asyncio.wait_for(
                self._message_queue.get(),
                timeout=timeout,
            )
        except TimeoutError:
            return False

        await self._deliver_to_consumer(queue_name, message)
        return True

    async def _deliver_to_consumer(self, queue: str, message: Message) -> None:
        """Route a message to the matching consumer callback."""
        for q, callback, _no_ack in self._consumers.values():
            if q == queue:
                try:
                    body = message.decode()
                except Exception:
                    body = message.body

                result = callback(body, message)
                if asyncio.iscoroutine(result):
                    await result
                return

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
        """
        await self._aio_channel.set_qos(prefetch_count=prefetch_count)

    async def basic_recover(self, requeue: bool = True) -> None:
        """Request the broker to redeliver all unacknowledged messages.

        aio-pika doesn't expose basic.recover directly, so we use the
        underlying aiormq channel's basic_recover method.

        Args:
            requeue: If True (default), the broker requeues messages so
                they may be delivered to other consumers. If False, they
                are redelivered to this consumer.
        """
        underlying = getattr(self._aio_channel, "channel", None)
        if underlying is None:
            logger.warning("Cannot recover: no underlying aiormq channel available")
            return
        if not hasattr(underlying, "basic_recover"):
            logger.warning("Cannot recover: aiormq channel does not support basic_recover")
            return
        await underlying.basic_recover(requeue=requeue)

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

        return Message(
            body=incoming.body,
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
            headers=dict(incoming.headers) if incoming.headers else {},
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

    #: RabbitMQ 3.3+ changed basic.qos semantics
    qos_semantics_matches_spec = False

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
        self._channels: list[Channel] = []
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return

        kwargs: dict[str, Any] = {}
        if "heartbeat" in self._options:
            kwargs["heartbeat"] = self._options["heartbeat"]

        self._connection = await aio_pika.connect(self._url, **kwargs)
        self._connected = True
        logger.debug("Connected to AMQP broker at %s", self._url)

    async def close(self) -> None:
        for channel in self._channels:
            await channel.close()
        self._channels.clear()

        if self._connection and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        self._connected = False

    async def create_channel(self) -> _Channel:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        if not self._connected:
            await self.connect()

        publisher_confirms = self._options.get("publisher_confirms", True)
        aio_channel = await self._connection.channel(  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
            publisher_confirms=publisher_confirms,
        )

        prefetch_count = self._options.get("prefetch_count", 0)
        if prefetch_count:
            await aio_channel.set_qos(prefetch_count=prefetch_count)

        channel = Channel(aio_channel)
        self._channels.append(channel)
        return channel

    @property
    def is_connected(self) -> bool:
        return self._connected and self._connection is not None and not self._connection.is_closed

    def driver_version(self) -> str:
        try:
            return aio_pika.__version__
        except AttributeError:
            return "N/A"
