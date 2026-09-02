"""Exchange and Queue declarations for pure asyncio Kombu."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .transport.base import Channel

__all__ = ("Exchange", "Queue", "binding")

TRANSIENT_DELIVERY_MODE = 1
PERSISTENT_DELIVERY_MODE = 2
DELIVERY_MODES = {
    "transient": TRANSIENT_DELIVERY_MODE,
    "persistent": PERSISTENT_DELIVERY_MODE,
}


def maybe_delivery_mode(
    v: int | str | None,
    modes: dict | None = None,
    default: int = PERSISTENT_DELIVERY_MODE,
) -> int:
    """Get delivery mode by name (or none if undefined)."""
    modes = DELIVERY_MODES if modes is None else modes
    if v:
        return v if isinstance(v, int) else modes[v]
    return default


class Exchange:
    """An Exchange declaration.

    Arguments:
        name: Name of the exchange. Default is '' (default exchange).
        type: Exchange type ('direct', 'fanout', 'topic'). Default is 'direct'.
        durable: Survive broker restart. Default is True.
        auto_delete: Delete when no queues bound. Default is False.
        delivery_mode: Default delivery mode for messages.
        arguments: Additional exchange arguments.
        no_declare: Never declare this exchange.

    In Redis, exchanges are emulated:
        - direct: Messages go to queue matching routing_key
        - fanout: Messages go to all bound queues (via pub/sub)
        - topic: Messages match queue patterns
    """

    TRANSIENT_DELIVERY_MODE = TRANSIENT_DELIVERY_MODE
    PERSISTENT_DELIVERY_MODE = PERSISTENT_DELIVERY_MODE

    def __init__(
        self,
        name: str = "",
        type: str = "direct",
        durable: bool = True,
        auto_delete: bool = False,
        delivery_mode: int | str | None = None,
        arguments: dict | None = None,
        no_declare: bool = False,
        channel: Channel | None = None,
    ):
        self.name = name
        self.type = type
        self.durable = durable
        self.auto_delete = auto_delete
        self.delivery_mode = maybe_delivery_mode(delivery_mode) if delivery_mode else None
        self.arguments = arguments or {}
        self.no_declare = no_declare
        self._channel = channel

    def __hash__(self) -> int:
        return hash(f"E|{self.name}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Exchange):
            return (
                self.name == other.name
                and self.type == other.type
                and self.arguments == other.arguments
                and self.durable == other.durable
                and self.auto_delete == other.auto_delete
            )
        return NotImplemented

    def __repr__(self) -> str:
        return f"<Exchange {self.name!r} type={self.type}>"

    def __str__(self) -> str:
        return f"Exchange {self.name!r}({self.type})"

    async def declare(self, channel: Channel | None = None) -> None:
        """Declare the exchange.

        For Redis, this is a no-op since Redis doesn't have real exchanges.
        We track exchange metadata for routing purposes.
        """
        ch = channel or self._channel
        if ch and not self.no_declare:
            await ch.declare_exchange(self)

    def bind(self, channel: Channel) -> Exchange:
        """Bind exchange to channel."""
        self._channel = channel
        return self


class Queue:
    """A Queue declaration.

    Arguments:
        name: Name of the queue. Default is '' (auto-generated).
        exchange: The Exchange the queue binds to.
        routing_key: The binding key. Interpretation depends on exchange type.
        durable: Survive broker restart. Default is True.
        exclusive: Only consumable by current connection. Default is False.
        auto_delete: Delete when all consumers finished. Default is False.
        queue_arguments: Arguments for queue declare.
        binding_arguments: Arguments for queue bind.
        consumer_arguments: Arguments for consume.
        no_declare: Never declare this queue.
        expires: Queue expiry time in seconds.
        message_ttl: Message TTL in seconds.
        max_length: Maximum number of messages.
        max_length_bytes: Maximum total size in bytes.
        max_priority: Enable priority queue with max priority level.
        no_ack: Don't require acknowledgment. Default is False.
    """

    def __init__(
        self,
        name: str = "",
        exchange: Exchange | str | None = None,
        routing_key: str = "",
        durable: bool = True,
        exclusive: bool = False,
        auto_delete: bool = False,
        queue_arguments: dict | None = None,
        binding_arguments: dict | None = None,
        consumer_arguments: dict | None = None,
        no_declare: bool = False,
        expires: float | None = None,
        message_ttl: float | None = None,
        max_length: int | None = None,
        max_length_bytes: int | None = None,
        max_priority: int | None = None,
        no_ack: bool = False,
        channel: Channel | None = None,
        alias: str | None = None,
        **kwargs: Any,
    ):
        self.name = name
        if isinstance(exchange, str):
            self.exchange = Exchange(exchange) if exchange else None
        else:
            self.exchange = exchange
        self.routing_key = routing_key or name
        self.durable = durable
        self.exclusive = exclusive
        self.auto_delete = auto_delete
        self.queue_arguments = queue_arguments or {}
        self.binding_arguments = binding_arguments or {}
        self.consumer_arguments = consumer_arguments or {}
        self.no_declare = no_declare
        self.expires = expires
        self.message_ttl = message_ttl
        self.max_length = max_length
        self.max_length_bytes = max_length_bytes
        self.max_priority = max_priority
        self.no_ack = no_ack
        self._channel = channel
        self.alias = alias

        # Build queue_arguments from convenience properties
        if expires is not None:
            self.queue_arguments["x-expires"] = int(expires * 1000)
        if message_ttl is not None:
            self.queue_arguments["x-message-ttl"] = int(message_ttl * 1000)
        if max_length is not None:
            self.queue_arguments["x-max-length"] = max_length
        if max_length_bytes is not None:
            self.queue_arguments["x-max-length-bytes"] = max_length_bytes
        if max_priority is not None:
            self.queue_arguments["x-max-priority"] = max_priority

    @classmethod
    def from_dict(
        cls,
        name: str,
        *,
        exchange: Exchange | str | None = None,
        exchange_type: str | None = None,
        routing_key: str | None = None,
        **kwargs: Any,
    ) -> Queue:
        """Create Queue from a dictionary of options.

        This is used for backward compatibility when adding queues from config.
        """
        if exchange is not None or exchange_type is not None:
            exchange_name = exchange if isinstance(exchange, str) else (exchange.name if exchange else "")
            exchange = Exchange(
                exchange_name or name,
                type=exchange_type or "direct",
            )
        return cls(
            name=name,
            exchange=exchange,
            routing_key=routing_key or name,
            **kwargs,
        )

    def as_dict(self, recurse: bool = False) -> dict[str, Any]:
        """Return Queue as a dictionary.

        Args:
            recurse: If True, also convert Exchange to dict.
        """
        d: dict[str, Any] = {
            "name": self.name,
            "routing_key": self.routing_key,
            "durable": self.durable,
            "exclusive": self.exclusive,
            "auto_delete": self.auto_delete,
            "no_ack": self.no_ack,
            "alias": self.alias,
            "queue_arguments": self.queue_arguments,
            "binding_arguments": self.binding_arguments,
        }
        if recurse and self.exchange:
            d["exchange"] = {
                "name": self.exchange.name,
                "type": self.exchange.type,
                "durable": self.exchange.durable,
                "auto_delete": self.exchange.auto_delete,
            }
        else:
            d["exchange"] = self.exchange
        return d

    def __hash__(self) -> int:
        return hash(f"Q|{self.name}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Queue):
            return self.name == other.name
        return NotImplemented

    def __repr__(self) -> str:
        return f"<Queue {self.name!r}>"

    def __str__(self) -> str:
        return f"Queue {self.name!r}"

    async def declare(self, channel: Channel | None = None) -> str:
        """Declare the exchange, the queue, and the binding between the two.

        A queue on its own receives nothing on a broker with real exchanges,
        so declaring one means declaring all three.

        Returns the queue name (useful for auto-generated names).
        """
        ch = channel or self._channel
        if ch and not self.no_declare:
            if self.exchange is not None:
                await self.exchange.declare(ch)
            name = await ch.declare_queue(self)
            await self.bind(ch)
            return name
        return self.name

    async def bind(self, channel: Channel | None = None) -> None:
        """Bind the queue to its exchange."""
        ch = channel or self._channel
        if ch and self.exchange:
            await ch.queue_bind(
                queue=self.name,
                exchange=self.exchange.name,
                routing_key=self.routing_key,
                arguments=self.binding_arguments,
            )

    async def get(
        self,
        channel: Channel | None = None,
        no_ack: bool | None = None,
        accept: set[str] | None = None,
    ) -> Any:
        """Get a single message from the queue.

        Returns None if queue is empty.
        """
        ch = channel or self._channel
        if ch:
            return await ch.get(
                self.name,
                no_ack=no_ack if no_ack is not None else self.no_ack,
                accept=accept,
            )
        return None

    async def purge(self, channel: Channel | None = None) -> int:
        """Remove all messages from the queue.

        Returns the number of messages deleted.
        """
        ch = channel or self._channel
        if ch:
            return await ch.queue_purge(self.name)
        return 0

    async def delete(
        self,
        channel: Channel | None = None,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete the queue.

        Returns the number of messages deleted.
        """
        ch = channel or self._channel
        if ch:
            return await ch.queue_delete(
                self.name,
                if_unused=if_unused,
                if_empty=if_empty,
            )
        return 0

    def bind_to_channel(self, channel: Channel) -> Queue:
        """Bind queue to channel."""
        self._channel = channel
        return self


class binding:
    """Represents a queue or exchange binding.

    Arguments:
        exchange: Exchange to bind to.
        routing_key: Routing key used as binding key.
        arguments: Arguments for bind operation.
        unbind_arguments: Arguments for unbind operation.
    """

    def __init__(
        self,
        exchange: Exchange | None = None,
        routing_key: str = "",
        arguments: dict | None = None,
        unbind_arguments: dict | None = None,
    ):
        self.exchange = exchange
        self.routing_key = routing_key
        self.arguments = arguments
        self.unbind_arguments = unbind_arguments

    async def declare(self, channel: Channel) -> None:
        """Declare destination exchange."""
        if self.exchange and self.exchange.name:
            await self.exchange.declare(channel=channel)

    async def bind(self, entity: Queue | Exchange, channel: Channel | None = None) -> None:
        """Bind entity to this binding."""
        if isinstance(entity, Queue):
            ch = channel or entity._channel
            if ch and self.exchange:
                await ch.queue_bind(
                    queue=entity.name,
                    exchange=self.exchange.name,
                    routing_key=self.routing_key,
                    arguments=self.arguments,
                )

    async def unbind(self, entity: Queue | Exchange, channel: Channel | None = None) -> None:
        """Unbind entity from this binding."""
        if isinstance(entity, Queue):
            ch = channel or entity._channel
            if ch and self.exchange:
                await ch.queue_unbind(
                    queue=entity.name,
                    exchange=self.exchange.name,
                    routing_key=self.routing_key,
                    arguments=self.unbind_arguments,
                )

    def __repr__(self) -> str:
        return f"<binding: {self}>"

    def __str__(self) -> str:
        ex_name = self.exchange.name if self.exchange else ""
        return f"{ex_name!r}->{self.routing_key!r}"
