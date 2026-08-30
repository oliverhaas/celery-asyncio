"""Base transport interface for pure asyncio Kombu.

All transports inherit from these base classes and implement
the async methods for their specific backend.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from kombu.exceptions import ChannelError, ConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue
    from kombu.message import Message

__all__ = ("Channel", "Transport")


class Channel(ABC):
    """Abstract base class for async channels.

    A channel represents a virtual connection within a transport.
    Each channel manages its own consumers and message delivery.
    """

    #: Set of consumer tags that don't require acknowledgment
    no_ack_consumers: set[str] | None = None

    @abstractmethod
    async def close(self) -> None:
        """Close the channel and clean up resources."""
        ...

    # Exchange operations

    @abstractmethod
    async def declare_exchange(self, exchange: Exchange) -> None:
        """Declare an exchange."""
        ...

    @abstractmethod
    async def exchange_delete(self, exchange: str) -> None:
        """Delete an exchange."""
        ...

    # Queue operations

    @abstractmethod
    async def declare_queue(self, queue: Queue) -> str:
        """Declare a queue.

        Returns the queue name (may be auto-generated).
        """
        ...

    @abstractmethod
    async def queue_bind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Bind a queue to an exchange."""
        ...

    @abstractmethod
    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Unbind a queue from an exchange."""
        ...

    @abstractmethod
    async def queue_purge(self, queue: str) -> int:
        """Purge all messages from a queue.

        Returns the number of messages purged.
        """
        ...

    @abstractmethod
    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete a queue.

        Returns the number of messages deleted.
        """
        ...

    # Message operations

    @abstractmethod
    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        """Publish a message to an exchange."""
        ...

    @abstractmethod
    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        """Get a single message from a queue.

        Returns None if queue is empty.
        """
        ...

    @abstractmethod
    async def basic_consume(
        self,
        queue: str,
        callback: Callable[[Message], Any],
        consumer_tag: str | None = None,
        no_ack: bool = False,
    ) -> str:
        """Register a consumer for a queue.

        Returns the consumer tag.
        """
        ...

    @abstractmethod
    async def basic_cancel(self, consumer_tag: str) -> None:
        """Cancel a consumer."""
        ...

    @abstractmethod
    async def drain_events(self, timeout: float | None = None) -> bool:
        """Wait for and deliver messages to consumers.

        Returns True if a message was delivered, False on timeout.
        """
        ...

    # Acknowledgment operations

    @abstractmethod
    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        """Acknowledge a message."""
        ...

    @abstractmethod
    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        """Reject a message."""
        ...

    @abstractmethod
    async def basic_recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages.

        Default implementation does nothing.
        """
        ...

    # Async context manager support

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class Transport(ABC):
    """Abstract base class for async transports.

    A transport is responsible for managing the connection to
    a message broker and creating channels.
    """

    #: The Channel class used by this transport
    Channel: type[Channel]

    #: Default port for this transport
    default_port: int | None = None

    #: Tuple of errors that can happen due to connection failure
    connection_errors: tuple[type[Exception], ...] = (ConnectionError,)

    #: Tuple of errors that can happen due to channel/method failure
    channel_errors: tuple[type[Exception], ...] = (ChannelError,)

    #: Subset of :attr:`channel_errors` for "another connection already holds
    #: this exclusive resource" (AMQP 405). Types rather than a reply code,
    #: since not every driver exposes one; empty where there is no exclusive
    #: ownership, so callers can catch it unconditionally.
    resource_locked_errors: tuple[type[Exception], ...] = ()

    #: Type of driver (e.g., 'redis', 'memory', 'amqp')
    driver_type: str = "N/A"

    #: Name of driver library (e.g., 'redis-py', 'memory')
    driver_name: str = "N/A"

    def __init__(self, url: str = "", **options: Any):
        """Initialize the transport.

        Args:
            url: Connection URL for the transport.
            **options: Additional transport-specific options.
        """
        self._url = url
        self._options = options

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the broker."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the transport and all channels."""
        ...

    @abstractmethod
    async def create_channel(self) -> Channel:
        """Create a new channel."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        ...

    def driver_version(self) -> str:
        """Return the version of the underlying driver."""
        return "N/A"

    # Async context manager support

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
