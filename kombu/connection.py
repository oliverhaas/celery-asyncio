"""Connection - Pure asyncio connection management for Kombu.

This module provides the Connection class for establishing connections
to message brokers using pure asyncio.

Example:
    async with Connection('redis://localhost') as conn:
        async with conn.Producer() as producer:
            await producer.publish({'hello': 'world'}, routing_key='my_queue')

    # Memory transport for testing
    async with Connection('memory://') as conn:
        ...
"""

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .log import get_logger
from .transport.base import Transport as BaseTransport  # noqa: TC001
from .utils.eventloop import default_loop_runner
from .utils.url import maybe_sanitize_url

if TYPE_CHECKING:
    from .entity import Queue

    # Aliased: the same-named methods below shadow these inside the class body,
    # which makes the bare names ambiguous in the return annotations.
    from .messaging import Consumer as _Consumer
    from .messaging import Producer as _Producer
    from .simple import SimpleQueue as _SimpleQueue
    from .transport.base import Channel

__all__ = ("Connection",)

logger = get_logger(__name__)

# Transport registry - uses Any to avoid circular imports
TRANSPORT_REGISTRY: dict[str, type[BaseTransport]] = {}


def _get_transport_class(scheme: str) -> type[BaseTransport]:
    """Get transport class for a URL scheme."""
    if not TRANSPORT_REGISTRY:
        # Lazy load transports
        from .transport.amqp import Transport as AMQPTransport
        from .transport.filesystem import Transport as FilesystemTransport
        from .transport.memory import Transport as MemoryTransport
        from .transport.valkey_redis import Transport as RedisTransport

        TRANSPORT_REGISTRY["amqp"] = AMQPTransport
        TRANSPORT_REGISTRY["amqps"] = AMQPTransport
        TRANSPORT_REGISTRY["valkey"] = RedisTransport
        TRANSPORT_REGISTRY["valkeys"] = RedisTransport
        TRANSPORT_REGISTRY["redis"] = RedisTransport
        TRANSPORT_REGISTRY["rediss"] = RedisTransport
        TRANSPORT_REGISTRY["memory"] = MemoryTransport
        TRANSPORT_REGISTRY["filesystem"] = FilesystemTransport

    if scheme not in TRANSPORT_REGISTRY:
        raise ValueError(
            f"Unsupported transport scheme: {scheme}. "
            f"Supported schemes: {', '.join(sorted(TRANSPORT_REGISTRY.keys()))}",
        )

    return TRANSPORT_REGISTRY[scheme]


class Connection:
    """A connection to a message broker.

    Pure asyncio implementation. All methods are async.

    Example:
        async with Connection('redis://localhost:6379') as conn:
            channel = await conn.channel()
            await channel.publish(b'hello', exchange='', routing_key='myqueue')

        # Memory transport (useful for testing)
        async with Connection('memory://') as conn:
            ...

    Arguments:
        hostname: Broker URL (e.g., 'redis://localhost:6379', 'memory://').

    Keyword Arguments:
        transport_options: Additional options for the transport.
    """

    def __init__(
        self,
        hostname: str = "redis://localhost:6379",
        transport_options: dict | None = None,
        **kwargs: Any,
    ):
        self._url = hostname
        self._transport_options = transport_options or {}
        self._transport: BaseTransport | None = None
        self._default_channel: Channel | None = None
        self._default_channel_lock = asyncio.Lock()
        self._closed = False

        # Parse URL and validate scheme
        parsed = urlparse(hostname)
        self._scheme = parsed.scheme
        # Validate by trying to get the transport class
        _get_transport_class(self._scheme)

    @property
    def transport(self) -> BaseTransport | None:
        """Get the transport."""
        return self._transport

    @property
    def is_connected(self) -> bool:
        """Check if connection is established."""
        return self._transport is not None and self._transport.is_connected

    async def connect(self) -> Connection:
        """Establish connection to the broker.

        Returns self for chaining.
        """
        self._closed = False
        if self._transport is None:
            transport_class = _get_transport_class(self._scheme)
            self._transport = transport_class(
                url=self._url,
                **self._transport_options,
            )
        await self._transport.connect()
        logger.debug("Connected to %s", self._url)
        return self

    async def close(self) -> None:
        """Close the connection.

        A close that fails leaves the connection open rather than closed: the
        transport is still there to try again on, where marking the connection
        closed first would have made a second :meth:`close` a no-op and left
        the transport running with nothing able to reach it.
        """
        if self._closed:
            return

        self._default_channel = None
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        self._closed = True

    async def channel(self) -> Channel:
        """Create a new channel.

        Returns a Channel object that can be used for messaging operations.
        """
        if not self.is_connected:
            await self.connect()
        ch = await self._transport.create_channel()  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        # Back-reference so channel.connection.client works
        ch.connection = self  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        return ch

    async def default_channel(self) -> Channel:
        """Get or create the default channel.

        The default channel is reused for convenience operations. Opening one
        is an await, so the check and the assignment are held under a lock:
        without it two concurrent first callers each opened a channel and one
        of them was left behind, still registered on the broker.
        """
        if self._default_channel is None:
            async with self._default_channel_lock:
                if self._default_channel is None:
                    self._default_channel = await self.channel()
        return self._default_channel

    def Producer(
        self,
        channel: Channel | None = None,
        **kwargs: Any,
    ) -> _Producer:
        """Create a Producer for this connection.

        Args:
            channel: Optional channel to use. If not provided,
                     the default channel will be used.
            **kwargs: Additional arguments passed to Producer.

        Returns:
            A Producer instance.
        """
        from .messaging import Producer

        return Producer(self, channel=channel, **kwargs)

    def Consumer(
        self,
        queues: list[Queue],
        channel: Channel | None = None,
        **kwargs: Any,
    ) -> _Consumer:
        """Create a Consumer for this connection.

        Args:
            queues: List of queues to consume from.
            channel: Optional channel to use.
            **kwargs: Additional arguments passed to Consumer.

        Returns:
            A Consumer instance.
        """
        from .messaging import Consumer

        return Consumer(self, queues=queues, channel=channel, **kwargs)

    def SimpleQueue(
        self,
        name: str,
        no_ack: bool | None = None,
        queue_opts: dict | None = None,
        exchange_opts: dict | None = None,
        channel: Channel | None = None,
        **kwargs: Any,
    ) -> _SimpleQueue:
        """Create a SimpleQueue for easy point-to-point messaging.

        Args:
            name: Queue name.
            no_ack: Don't require message acknowledgment.
            queue_opts: Options passed to Queue declaration.
            exchange_opts: Options passed to Exchange declaration.
            channel: Optional channel to use.
            **kwargs: Additional arguments.

        Returns:
            A SimpleQueue instance.
        """
        from .simple import SimpleQueue

        return SimpleQueue(
            self,
            name=name,
            no_ack=no_ack,
            queue_opts=queue_opts,
            exchange_opts=exchange_opts,
            channel=channel,
            **kwargs,
        )

    async def drain_events(self, timeout: float | None = None) -> None:
        """Wait for a single event from the broker.

        This will block until a message arrives or timeout is reached.

        Args:
            timeout: Maximum time to wait in seconds.

        Raises:
            socket.timeout: If timeout is reached with no events.
        """

        channel = await self.default_channel()
        delivered = await channel.drain_events(timeout=timeout)
        if not delivered and timeout is not None:
            raise TimeoutError("timed out")

    async def ensure_connection(
        self,
        errback: Any = None,
        max_retries: int | None = None,
        interval_start: float = 2.0,
        interval_step: float = 2.0,
        interval_max: float = 30.0,
        callback: Any = None,
    ) -> Connection:
        """Ensure we have a connection to the broker.

        Will reconnect if connection is lost.

        Args:
            errback: Optional callback called on each retry with (exc, interval).
            max_retries: Maximum number of retries (None = unlimited).
            interval_start: Initial retry interval.
            interval_step: Interval increase per retry.
            interval_max: Maximum retry interval.
            callback: Optional callback called between retries (e.g., for shutdown checks).

        Returns:
            self
        """
        retries = 0
        interval = interval_start

        while True:
            try:
                await self.connect()
                return self
            except Exception as exc:
                if max_retries is not None and retries >= max_retries:
                    raise

                if errback:
                    errback(exc, interval)

                if callback:
                    # No arguments: this is upstream's between-retries hook, and
                    # celery passes `maybe_shutdown`, which takes none. Calling it
                    # like an errback turned every retry into a TypeError that
                    # killed the worker outright.
                    callback()

                logger.warning(
                    "Connection failed, retrying in %.2fs: %r",
                    interval,
                    exc,
                )
                await asyncio.sleep(interval)

                retries += 1
                interval = min(interval + interval_step, interval_max)

    def clone(self, **kwargs: Any) -> Connection:
        """Create a copy of this connection with optional overrides.

        Args:
            **kwargs: Override connection parameters.

        Returns:
            A new Connection instance.
        """
        return Connection(
            hostname=kwargs.pop("hostname", self._url),
            transport_options=kwargs.pop("transport_options", self._transport_options.copy()),
            **kwargs,
        )

    async def release(self) -> None:
        """Release the connection (alias for close)."""
        await self.close()

    async def __aenter__(self) -> Connection:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    def __enter__(self) -> Connection:
        """Sync context manager entry (for compatibility with sync code like Flower)."""
        # Shared loop, not a throwaway one: a transport belongs to the loop that
        # opened it, and __exit__ has to reach that same loop.
        default_loop_runner().run_from_any_thread(self.connect())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Sync context manager exit (for compatibility with sync code like Flower)."""
        default_loop_runner().run_from_any_thread(self.close())

    def __repr__(self) -> str:
        return f"<Connection: {self._url} connected={self.is_connected}>"

    # Connection error tuples

    @property
    def _transport_cls(self) -> type[BaseTransport]:
        """The transport class for this connection's URL.

        The error tuples below are class attributes, so they can be read
        before anything is connected, which they have to be. A caller asking
        which errors are recoverable is usually about to connect for the first
        time, or has just been disconnected (:meth:`close` drops
        ``_transport``); answering with a generic default in either case sends
        transport-specific connection errors, such as aiormq's
        ``AMQPConnectionError``, straight past the handler that was meant to
        retry them.
        """
        if self._transport is not None:
            return type(self._transport)
        return _get_transport_class(self._scheme)

    @property
    def connection_errors(self) -> tuple[type[Exception], ...]:
        """Tuple of connection exceptions.

        These are exceptions that indicate the connection was lost
        and the operation should be retried.
        """
        return self._transport_cls.connection_errors

    @property
    def channel_errors(self) -> tuple[type[Exception], ...]:
        """Tuple of channel exceptions.

        These are exceptions that indicate the channel is broken
        but the connection itself may be fine.
        """
        return self._transport_cls.channel_errors

    @property
    def resource_locked_errors(self) -> tuple[type[Exception], ...]:
        """Tuple of exceptions meaning an exclusive resource is already held.

        A subset of :attr:`channel_errors`; empty for transports that have no
        notion of exclusive ownership.
        """
        return self._transport_cls.resource_locked_errors

    def as_uri(self, include_password: bool = False) -> str:
        """Return the connection URI, with password masked by default.

        Args:
            include_password: If True, include the actual password.

        Returns:
            Connection URI string.
        """
        if include_password:
            return self._url
        return maybe_sanitize_url(self._url)

    def info(self) -> dict[str, Any]:
        """Return connection info as a dict.

        Returns:
            Dictionary with connection details.
        """
        parsed = urlparse(self._url)
        transport = self._transport
        return {
            "hostname": parsed.hostname or "localhost",
            "port": parsed.port or (transport.default_port if transport else None),
            "transport": self._scheme,
            "is_connected": self.is_connected,
            "uri": self.as_uri(),
            "driver_type": transport.driver_type if transport else self._scheme,
            "driver_name": transport.driver_name if transport else self._scheme,
        }

    def supports_exchange_type(self, exchange_type: str) -> bool:
        """Check if the transport supports a given exchange type.

        Args:
            exchange_type: Exchange type (e.g., 'direct', 'fanout', 'topic').

        Returns:
            True if the exchange type is supported.
        """
        if self._transport is not None:
            return exchange_type in getattr(
                self._transport,
                "exchange_types",
                {"direct", "fanout", "topic"},
            )
        # Default: assume all standard types are supported
        return exchange_type in {"direct", "fanout", "topic"}

    # Aliases for backwards compatibility concepts
    @property
    def client(self) -> Connection:
        """Self-reference for old-style channel.connection.client access."""
        return self

    @property
    def connected(self) -> bool:
        """Alias for is_connected."""
        return self.is_connected
