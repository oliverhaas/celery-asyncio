"""Sending and receiving messages - Pure asyncio implementation."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from .compression import compress
from .entity import Exchange, Queue
from .log import get_logger
from .serialization import dumps, prepare_accept_content
from .utils.json import dumps as json_dumps

logger = get_logger(__name__)

if TYPE_CHECKING:
    from .connection import Connection
    from .message import Message
    from .transport.base import Channel

__all__ = ("Consumer", "Producer")


async def _maybe_await(result: Any) -> Any:
    return await result if asyncio.iscoroutine(result) else result


class Producer:
    """Message Producer - Pure asyncio implementation.

    Arguments:
        connection: The connection to use.
        channel: Optional channel. If not provided, uses connection's channel.
        exchange: Default exchange for publishing.
        routing_key: Default routing key.
        serializer: Default serializer. Default is 'json'.
        compression: Default compression method. Disabled by default.
        auto_declare: Automatically declare the exchange. Default is True.

    Example:
        async with connection.Producer() as producer:
            await producer.publish({'hello': 'world'}, routing_key='my_queue')
    """

    exchange: Exchange | None = None
    routing_key: str = ""
    serializer: str | None = None
    compression: str | None = None
    auto_declare: bool = True

    def __init__(
        self,
        connection: Connection,
        channel: Channel | None = None,
        exchange: Exchange | str | None = None,
        routing_key: str | None = None,
        serializer: str | None = None,
        compression: str | None = None,
        auto_declare: bool | None = None,
    ):
        self._connection = connection
        self._channel = channel
        self._declared = False

        if isinstance(exchange, str):
            self.exchange = Exchange(exchange) if exchange else Exchange("")
        elif exchange is not None:
            self.exchange = exchange
        else:
            self.exchange = Exchange("")

        self.routing_key = routing_key if routing_key is not None else self.routing_key
        self.serializer = serializer if serializer is not None else self.serializer
        self.compression = compression if compression is not None else self.compression

        if auto_declare is not None:
            self.auto_declare = auto_declare

    async def _ensure_channel(self) -> Channel:
        """Ensure we have a channel."""
        if self._channel is None:
            self._channel = await self._connection.default_channel()
        return self._channel

    async def declare(self) -> None:
        """Declare the exchange."""
        if self._declared:
            return
        if self.exchange and self.exchange.name:
            channel = await self._ensure_channel()
            await self.exchange.declare(channel)
        self._declared = True

    async def publish(
        self,
        body: Any,
        routing_key: str | None = None,
        exchange: Exchange | str | None = None,
        serializer: str | None = None,
        compression: str | None = None,
        headers: dict | None = None,
        priority: int | None = None,
        expiration: float | None = None,
        delivery_mode: int | None = None,
        declare: list | None = None,
        retry: bool = False,
        retry_policy: dict | None = None,
        **kwargs: Any,
    ) -> None:
        """Publish a message.

        Args:
            body: Message body (will be serialized).
            routing_key: Routing key. Uses default if not specified.
            exchange: Exchange to publish to. Uses default if not specified.
            serializer: Serializer to use. Uses default if not specified.
            compression: Compression method. Uses default if not specified.
            headers: Optional message headers.
            priority: Message priority (0-9).
            expiration: Message TTL in seconds.
            delivery_mode: 1=transient, 2=persistent.
            declare: List of Exchange/Queue objects to declare before publishing.
            retry: Retry the publish if the connection or the channel fails.
            retry_policy: Options for the retry: ``max_retries``,
                ``interval_start``, ``interval_step``, ``interval_max`` and
                ``errback``, as taken by :meth:`ensure`.
            **kwargs: Additional properties.
        """
        attempt = partial(
            self._publish,
            body,
            routing_key=routing_key,
            exchange=exchange,
            serializer=serializer,
            compression=compression,
            headers=headers,
            priority=priority,
            expiration=expiration,
            delivery_mode=delivery_mode,
            declare=declare,
            **kwargs,
        )
        if not retry:
            await attempt()
            return
        await self.ensure(attempt, **(retry_policy or {}))

    async def ensure(
        self,
        attempt: Callable[[], Awaitable[None]],
        max_retries: int | None = None,
        interval_start: float = 2.0,
        interval_step: float = 2.0,
        interval_max: float = 30.0,
        errback: Callable[[Exception, float], None] | None = None,
    ) -> None:
        """Call `attempt` again whenever the broker breaks under it.

        Args:
            attempt: The operation to run, and to run again after a reconnect.
            max_retries: How many times to retry, or None to retry forever.
            interval_start: Seconds to wait before the first retry.
            interval_step: Seconds added to the wait after each retry.
            interval_max: Longest wait between two retries.
            errback: Called with (exc, interval) before each wait.
        """
        recoverable = self._connection.connection_errors + self._connection.channel_errors
        retries = 0
        interval = interval_start

        while True:
            try:
                await attempt()
                return
            except recoverable as exc:
                if max_retries is not None and retries >= max_retries:
                    raise

                if errback is not None:
                    errback(exc, interval)

                logger.warning("Publish failed, retrying in %.2fs: %r", interval, exc)
                await asyncio.sleep(interval)

                retries += 1
                interval = min(interval + interval_step, interval_max)
                await self.revive()

    async def revive(self) -> None:
        """Reconnect and start over on a new channel.

        The broker state this producer built up is gone with the connection, so
        the exchange is declared again on the next publish.
        """
        self._channel = None
        self._declared = False
        await self._connection.close()
        await self._connection.connect()

    async def _publish(
        self,
        body: Any,
        routing_key: str | None = None,
        exchange: Exchange | str | None = None,
        serializer: str | None = None,
        compression: str | None = None,
        headers: dict | None = None,
        priority: int | None = None,
        expiration: float | None = None,
        delivery_mode: int | None = None,
        declare: list | None = None,
        **kwargs: Any,
    ) -> None:
        channel = await self._ensure_channel()

        # Auto declare
        if self.auto_declare and not self._declared:
            await self.declare()

        # A declare that fails means the entity on the broker does not match the
        # one being published to, which the caller has to hear about: the channel
        # is dead afterwards anyway.
        if declare:
            for entity in declare:
                await entity.declare(channel)

        # Resolve defaults
        routing_key = routing_key if routing_key is not None else self.routing_key
        serializer = serializer if serializer is not None else (self.serializer or "json")
        compression = compression if compression is not None else self.compression
        # Copied so the envelope keys below do not leak into a headers dict the
        # caller reuses for the next publish.
        headers = {} if headers is None else dict(headers)

        if isinstance(exchange, str):
            exchange_name = exchange
        elif exchange is not None:
            exchange_name = exchange.name
        elif self.exchange:
            exchange_name = self.exchange.name
        else:
            exchange_name = ""

        # Serialize the body
        content_type, content_encoding, serialized_body = dumps(body, serializer)

        if compression:
            serialized_body, headers["compression"] = compress(serialized_body, compression)

        # Build message envelope
        properties = {
            **kwargs,
        }
        if priority is not None:
            properties["priority"] = priority
        if expiration is not None:
            properties["expiration"] = str(int(expiration * 1000))
        if delivery_mode is not None:
            properties["delivery_mode"] = delivery_mode

        # Encode body for JSON envelope
        body_str = serialized_body
        if isinstance(serialized_body, bytes):
            if compression:
                # Compressed bytes are not text, even when they happen to decode.
                body_str = None
            else:
                try:
                    body_str = serialized_body.decode(content_encoding or "utf-8")
                except UnicodeDecodeError, LookupError:  # fmt: skip
                    # Binary serializers (pickle, msgpack) produce non-UTF-8 bytes
                    body_str = None
            if body_str is None:
                body_str = base64.b64encode(serialized_body).decode("ascii")
                headers["body_encoding"] = "base64"

        message = {
            "body": body_str,
            "content-type": content_type,
            "content-encoding": content_encoding,
            "properties": properties,
            "headers": headers,
        }

        # Encode and publish
        message_bytes = json_dumps(message).encode("utf-8")
        await channel.publish(
            message=message_bytes,
            exchange=exchange_name,
            routing_key=routing_key,
        )

    async def close(self) -> None:
        """Close the producer."""
        # Channel is managed by connection, don't close it

    async def __aenter__(self) -> Producer:
        """Async context manager entry."""
        await self._ensure_channel()
        if self.auto_declare:
            await self.declare()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    def __repr__(self) -> str:
        return f"<Producer: {self._connection}>"


class Consumer:
    """Message Consumer - Pure asyncio implementation.

    Arguments:
        connection: The connection to use.
        queues: List of queues to consume from.
        channel: Optional channel. If not provided, uses connection's channel.
        callbacks: List of callbacks to call when message is received.
        no_ack: Don't require message acknowledgment. Default is False.
        accept: List of accepted content types.
        prefetch_count: Number of messages to prefetch, applied when
            consuming starts.
        on_message: Called with the raw message instead of the callbacks.
        on_decode_error: Called with (message, exc) when a body will not
            decode. Without one the error reaches the caller draining events.

    Example:
        async with connection.Consumer([queue], callbacks=[on_message]):
            await connection.drain_events(timeout=1.0)
    """

    def __init__(
        self,
        connection: Connection | Channel,
        queues: list[Queue] | None = None,
        channel: Channel | None = None,
        callbacks: list[Callable] | None = None,
        no_ack: bool = False,
        accept: list[str] | None = None,
        prefetch_count: int | None = None,
        on_message: Callable | None = None,
        on_decode_error: Callable | None = None,
    ):
        # Accept either a Connection or a Channel as the first argument.
        # If a Channel is passed (has basic_consume but not default_channel),
        # use it directly instead of going through Connection.default_channel().
        self._channel: Channel | None
        if hasattr(connection, "basic_consume") and not hasattr(connection, "default_channel"):
            self._connection = getattr(connection, "connection", None)
            self._channel = channel or connection
        else:
            self._connection = connection
            self._channel = channel
        self._queues = queues or []
        self._callbacks = callbacks or []
        # on_message receives just (message,): the raw message, no body decode.
        # Regular callbacks receive (body, message).
        self._on_message_callback = on_message
        self._no_ack = no_ack
        self._accept = prepare_accept_content(set(accept)) if accept else None
        self._prefetch_count = prefetch_count
        self._consumer_tags: dict[str, str] = {}
        self._running = False
        self._declared: set[str] = set()
        self.on_decode_error = on_decode_error

    @property
    def queues(self) -> list[Queue]:
        """Get the list of queues."""
        return self._queues

    def register_callback(self, callback: Callable) -> None:
        """Register a callback to be called when a message is received."""
        self._callbacks.append(callback)

    async def _ensure_channel(self) -> Channel:
        """Ensure we have a channel."""
        if self._channel is None:
            self._channel = await self._connection.default_channel()  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute,call-non-callable]
        return self._channel

    async def declare(self) -> None:
        """Declare the queues that have not been declared yet."""
        channel = await self._ensure_channel()
        for queue in self._queues:
            if queue.name in self._declared:
                continue
            await queue.declare(channel)
            self._declared.add(queue.name)

    async def consume(self) -> None:
        """Start consuming from every queue that has no broker consumer yet.

        Called again after :meth:`add_queue`, it declares and consumes the new
        queue and leaves the queues it is already consuming alone.
        """
        channel = await self._ensure_channel()

        if self._prefetch_count is not None and not self._running:
            # Before the first basic_consume: the broker applies a prefetch
            # count to the consumers registered after it, not before.
            await self.qos(prefetch_count=self._prefetch_count)

        await self.declare()

        for queue in self._queues:
            if queue.name in self._consumer_tags:
                continue
            self._consumer_tags[queue.name] = await channel.basic_consume(
                queue=queue.name,
                callback=self._on_message,
                no_ack=self._no_ack,
            )

        self._running = True

    async def _on_message(self, body: Any, message: Message) -> None:
        """Handle received message."""
        if self._accept is not None:
            # The transport decoded the body before it could know what this
            # consumer accepts, so hand the message the restriction and let it
            # decode again under it.
            message.accept = self._accept

        if self._on_message_callback is not None:
            # Raw message callback: it receives the message and decodes if it
            # wants to, which is where `accept` is then enforced.
            await _maybe_await(self._on_message_callback(message))
            return

        try:
            if message.errors:
                message._reraise_error()
            # The transport hands over whatever it could make of the body and
            # keeps a failed decode to itself, so the decode is repeated here,
            # a cache hit when it worked, to have the failure to report.
            body = message.decode()
        except Exception as exc:
            if self.on_decode_error is None:
                raise
            await _maybe_await(self.on_decode_error(message, exc))
            return

        for callback in self._callbacks:
            await _maybe_await(callback(body, message))

    async def cancel(self) -> None:
        """Cancel consuming."""
        self._running = False

        if self._channel:
            for tag in self._consumer_tags.values():
                await self._channel.basic_cancel(tag)
        self._consumer_tags.clear()

    async def qos(self, prefetch_count: int = 0) -> None:
        """Set the prefetch count on this consumer's channel."""
        channel = await self._ensure_channel()
        await channel.basic_qos(prefetch_count=prefetch_count)

    async def purge(self) -> int:
        """Purge all queues.

        Returns the total number of messages purged.
        """
        total = 0
        channel = await self._ensure_channel()
        for queue in self._queues:
            total += await channel.queue_purge(queue.name)
        return total

    def add_queue(self, queue: Queue) -> None:
        """Add a queue to consume from."""
        if queue not in self._queues:
            self._queues.append(queue)

    def consuming_from(self, queue_name: str | Queue) -> bool:
        """Check if currently consuming from the given queue."""
        name = queue_name if isinstance(queue_name, str) else queue_name.name
        return any(q.name == name for q in self._queues)

    async def cancel_by_queue(self, queue: str | Queue) -> None:
        """Stop consuming from one queue, leaving the others running."""
        name = queue if isinstance(queue, str) else queue.name
        tag = self._consumer_tags.pop(name, None)
        if tag is not None and self._channel is not None:
            await self._channel.basic_cancel(tag)
        self._queues = [q for q in self._queues if q.name != name]
        self._declared.discard(name)

    async def close(self) -> None:
        """Close the consumer."""
        await self.cancel()

    async def __aenter__(self) -> Consumer:
        """Async context manager entry."""
        await self.consume()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    def __repr__(self) -> str:
        return f"<Consumer: {len(self._queues)} queues on {self._connection}>"
