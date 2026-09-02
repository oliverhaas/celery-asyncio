"""Simple messaging interface - Pure asyncio implementation."""

import asyncio
from collections import deque
from queue import Empty
from typing import TYPE_CHECKING, Any

from .entity import Exchange, Queue
from .serialization import prepare_accept_content

if TYPE_CHECKING:
    from .connection import Connection
    from .message import Message
    from .transport.base import Channel

__all__ = ("SimpleBuffer", "SimpleQueue")


class SimpleQueue:
    """Simple API for persistent queues - Pure asyncio implementation.

    Provides a simplified interface for point-to-point messaging
    using put/get operations similar to Python's Queue.

    Example:
        async with connection.SimpleQueue('my_queue') as queue:
            await queue.put({'hello': 'world'})
            message = await queue.get(timeout=5)
            await message.ack()

    Arguments:
        connection: The connection to use.
        name: Queue name (also used as exchange and routing key).
        no_ack: Don't require message acknowledgment.
        queue_opts: Options passed to Queue declaration.
        exchange_opts: Options passed to Exchange declaration.
        serializer: Default serializer for messages.
        compression: Default compression for messages.
        accept: List of accepted content types for consuming.
    """

    Empty = Empty
    no_ack: bool = False
    queue_opts: dict = {}
    exchange_opts: dict = {"type": "direct"}

    def __init__(
        self,
        connection: Connection,
        name: str | Queue,
        no_ack: bool | None = None,
        queue_opts: dict | None = None,
        exchange_opts: dict | None = None,
        serializer: str | None = None,
        compression: str | None = None,
        accept: list[str] | None = None,
        channel: Channel | None = None,
        **kwargs: Any,
    ):
        self._connection = connection
        self._channel = channel
        self._buffer: deque[Message] = deque()
        self._consuming = False
        self._consumer_tag: str | None = None

        if no_ack is None:
            no_ack = self.no_ack
        self._no_ack = no_ack

        _queue_opts = dict(self.queue_opts, **(queue_opts or {}))
        _exchange_opts = dict(self.exchange_opts, **(exchange_opts or {}))

        if isinstance(name, Queue):
            self._queue = name
            self._exchange = name.exchange
            self._routing_key = name.routing_key
        else:
            self._exchange = Exchange(name, **_exchange_opts)
            self._queue = Queue(
                name,
                exchange=self._exchange,
                routing_key=name,
                **_queue_opts,
            )
            self._routing_key = name

        self._serializer = serializer
        self._compression = compression
        self._accept = prepare_accept_content(accept)
        self._declared = False

    async def _ensure_channel(self) -> Channel:
        """Ensure we have a channel."""
        if self._channel is None:
            self._channel = await self._connection.default_channel()
        return self._channel

    async def _declare(self) -> None:
        """Declare exchange and queue."""
        if self._declared:
            return

        channel = await self._ensure_channel()

        if self._exchange:
            await self._exchange.declare(channel)

        await self._queue.declare(channel)

        if self._exchange:
            await self._queue.bind(channel)

        self._declared = True

    async def _consume(self) -> None:
        """Start consuming if not already."""
        if self._consuming:
            return

        await self._declare()
        channel = await self._ensure_channel()

        self._consumer_tag = await channel.basic_consume(
            queue=self._queue.name,
            callback=self._receive,
            no_ack=self._no_ack,
        )
        self._consuming = True

    def _receive(self, body: Any, message: Message) -> None:
        """Callback for received messages."""
        if self._accept is not None:
            # get_nowait() hands `accept` to the transport, which cannot be
            # reached from here, so the restriction goes on the message.
            message.accept = self._accept
        self._buffer.append(message)

    async def put(
        self,
        message: Any,
        serializer: str | None = None,
        headers: dict | None = None,
        compression: str | None = None,
        routing_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Put a message on the queue.

        Args:
            message: Message body (will be serialized).
            serializer: Serializer to use.
            headers: Optional message headers.
            compression: Compression method.
            routing_key: Override routing key.
            **kwargs: Additional message properties.
        """
        await self._declare()

        from .messaging import Producer

        producer = Producer(
            self._connection,
            channel=self._channel,
            exchange=self._exchange,
            routing_key=routing_key or self._routing_key,
            serializer=serializer or self._serializer,
            compression=compression or self._compression,
        )

        await producer.publish(
            body=message,
            headers=headers,
            **kwargs,
        )

    async def get(
        self,
        block: bool = True,
        timeout: float | None = None,
    ) -> Message:
        """Get a message from the queue.

        Args:
            block: If True, block until a message is available.
            timeout: Maximum time to wait in seconds.

        Returns:
            The message object.

        Raises:
            Empty: If no message is available and block is False or timeout expired.
        """
        if not block:
            return await self.get_nowait()

        await self._consume()

        start_time = asyncio.get_event_loop().time()
        remaining = timeout

        while True:
            if self._buffer:
                return self._buffer.popleft()

            if remaining is not None and remaining <= 0.0:
                raise self.Empty

            try:
                channel = await self._ensure_channel()
                delivered = await channel.drain_events(timeout=min(remaining or 1.0, 1.0))
                if delivered and self._buffer:
                    return self._buffer.popleft()
            except TimeoutError:
                pass  # drain_events timed out, loop and check remaining

            if timeout is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                remaining = timeout - elapsed

    async def get_nowait(self) -> Message:
        """Get a message without blocking.

        Returns:
            The message object.

        Raises:
            Empty: If no message is available.
        """
        await self._declare()

        # First check buffer
        if self._buffer:
            return self._buffer.popleft()

        # Try to get directly from queue
        channel = await self._ensure_channel()
        message = await channel.get(
            self._queue.name,
            no_ack=self._no_ack,
            accept=self._accept,
        )

        if message is None:
            raise self.Empty

        return message

    async def clear(self) -> int:
        """Purge all messages from the queue.

        Returns:
            Number of messages purged.
        """
        await self._declare()
        channel = await self._ensure_channel()
        return await channel.queue_purge(self._queue.name)

    async def qsize(self) -> int:
        """Get the approximate number of messages in the queue.

        Raises:
            NotImplementedError: This method is not supported across all
                transports. Use transport-specific APIs for queue size.
        """
        raise NotImplementedError(
            "qsize() is not supported in a transport-agnostic way. Use transport-specific APIs to query queue depth.",
        )

    async def close(self) -> None:
        """Close the simple queue."""
        if self._consumer_tag and self._channel:
            await self._channel.basic_cancel(self._consumer_tag)
            self._consumer_tag = None
        self._consuming = False
        self._buffer.clear()

    async def __aenter__(self) -> SimpleQueue:
        """Async context manager entry."""
        await self._declare()
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
        return f"<SimpleQueue: {self._queue.name}>"


class SimpleBuffer(SimpleQueue):
    """Simple API for ephemeral queues - Pure asyncio implementation.

    Like SimpleQueue but with transient, auto-delete settings.
    Suitable for temporary communication channels.
    """

    no_ack: bool = True
    queue_opts: dict = {
        "durable": False,
        "auto_delete": True,
        "exclusive": True,
    }
    exchange_opts: dict = {
        "type": "direct",
        "durable": False,
        "auto_delete": True,
    }
