"""Sending and receiving messages - Pure asyncio implementation."""

import asyncio
import base64
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .entity import Exchange, Queue
from .log import get_logger
from .serialization import dumps
from .utils.json import dumps as json_dumps

logger = get_logger(__name__)

if TYPE_CHECKING:
    from .connection import Connection
    from .message import Message
    from .transport.base import Channel

__all__ = ("Consumer", "Producer")


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
        **kwargs: Any,
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
            retry: Whether to retry on failure.
            retry_policy: Retry policy options.
            **kwargs: Additional properties.
        """
        channel = await self._ensure_channel()

        # Auto declare
        if self.auto_declare and not self._declared:
            await self.declare()

        # Declare any extra exchanges/queues
        if declare:
            for entity in declare:
                if hasattr(entity, "declare"):
                    try:
                        await entity.declare(channel)
                    except Exception as exc:
                        logger.debug("Failed to declare %r: %s", entity, exc)

        # Resolve defaults
        routing_key = routing_key if routing_key is not None else self.routing_key
        serializer = serializer if serializer is not None else (self.serializer or "json")

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
        if isinstance(serialized_body, bytes):
            try:
                body_str = serialized_body.decode(content_encoding or "utf-8")
            except UnicodeDecodeError, LookupError:  # fmt: skip
                # Binary serializers (pickle, msgpack) produce non-UTF-8 bytes
                body_str = base64.b64encode(serialized_body).decode("ascii")
                if headers is None:
                    headers = {}
                headers["body_encoding"] = "base64"
        else:
            body_str = serialized_body

        message = {
            "body": body_str,
            "content-type": content_type,
            "content-encoding": content_encoding,
            "properties": properties,
            "headers": headers or {},
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
        prefetch_count: Number of messages to prefetch. Not implemented yet.

    Example:
        async with connection.Consumer([queue], callbacks=[on_message]) as consumer:
            async for _ in consumer:
                pass  # Messages delivered via callbacks
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
        **kwargs: Any,
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
        # on_message receives just (message,) — raw message, no body decode.
        # Regular callbacks receive (body, message).
        self._on_message_callback = on_message
        self._no_ack = no_ack
        self._accept = set(accept) if accept else None
        self._prefetch_count = prefetch_count
        self._consumer_tags: list[str] = []
        self._running = False
        self._declared = False
        self._iter_timeout: float = 1.0
        self.on_decode_error = kwargs.get("on_decode_error")

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
        """Declare all queues and their exchanges."""
        if self._declared:
            return

        channel = await self._ensure_channel()
        for queue in self._queues:
            if queue.exchange:
                await queue.exchange.declare(channel)
            await queue.declare(channel)
            if queue.exchange:
                await queue.bind(channel)

        self._declared = True

    async def consume(self) -> None:
        """Start consuming from queues."""
        channel = await self._ensure_channel()

        # Declare queues if not already done
        await self.declare()

        # Register consumers
        for queue in self._queues:
            tag = await channel.basic_consume(
                queue=queue.name,
                callback=self._on_message,
                no_ack=self._no_ack,
            )
            self._consumer_tags.append(tag)

        self._running = True

    async def _on_message(self, body: Any, message: Message) -> Any:
        """Handle received message."""
        if self._on_message_callback:
            # Raw message callback — receives just the message (no body decode)
            result = self._on_message_callback(message)
            if asyncio.iscoroutine(result):
                await result
            return
        for callback in self._callbacks:
            result = callback(body, message)
            if asyncio.iscoroutine(result):
                await result

    async def cancel(self) -> None:
        """Cancel consuming."""
        self._running = False

        if self._channel:
            for tag in self._consumer_tags:
                await self._channel.basic_cancel(tag)
        self._consumer_tags.clear()

    async def recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages."""
        if self._channel:
            await self._channel.basic_recover(requeue=requeue)

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

    async def cancel_by_queue(self, queue_name: str | Queue) -> None:
        """Cancel consuming from a specific queue."""
        name = queue_name if isinstance(queue_name, str) else queue_name.name
        self._queues = [q for q in self._queues if q.name != name]

    async def close(self) -> None:
        """Close the consumer."""
        await self.cancel()

    def __aiter__(self) -> Consumer:
        """Return async iterator."""
        return self

    async def __anext__(self) -> None:
        """Async iteration - wait for and deliver messages.

        Messages are delivered via callbacks, this yields None.
        """
        if not self._running:
            raise StopAsyncIteration

        try:
            drainer = self._connection or self._channel
            await drainer.drain_events(timeout=self._iter_timeout)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        except TimeoutError:
            pass

    async def iterate(
        self,
        limit: int | None = None,
        timeout: float | None = None,
    ):
        """Async generator for consuming messages.

        Args:
            limit: Maximum number of messages to consume.
            timeout: Overall timeout in seconds.

        Yields:
            None after each message is delivered (messages go to callbacks).
        """
        count = 0
        start_time = asyncio.get_event_loop().time() if timeout else None

        while True:
            if limit is not None and count >= limit:
                break

            if timeout and start_time:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= timeout:
                    break

            try:
                drainer = self._connection or self._channel
                await drainer.drain_events(timeout=1.0)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
                count += 1
                yield
            except TimeoutError:
                yield

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
