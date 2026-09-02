"""Pure asyncio in-memory transport for Kombu.

Messages are held in process-wide deques, so every ``memory://`` connection in
the process shares them. The deques carry no event loop affinity: a producer
running under one event loop (or thread) and a consumer running under another
can exchange messages.

Features
========
* Type: In-memory
* Supports Direct: Yes
* Supports Topic: Yes
* Supports Fanout: Yes
* Supports Priority: No
* Supports TTL: No

Connection String
=================
.. code-block::

    memory://
"""

import asyncio
import re
import threading
import uuid
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, ClassVar

from kombu.exceptions import KombuError
from kombu.log import get_logger
from kombu.message import Message

from .base import Channel as BaseChannel
from .base import Transport as BaseTransport
from .base import decode_envelope

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop, Future
    from collections.abc import Callable, Iterable
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue

__all__ = ("Channel", "Transport")

logger = get_logger("kombu.transport.memory")

#: Longest single wait in drain_events, so a blocked drain notices new consumers.
MAX_WAIT = 1.0


def _resolve(waiter: Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def _wake(loop: AbstractEventLoop, waiter: Future[None]) -> None:
    """Resolve a waiter from whichever thread published the message."""
    if waiter.done() or loop.is_closed():
        return
    loop.call_soon_threadsafe(_resolve, waiter)


class Channel(BaseChannel):
    """Pure asyncio in-memory channel.

    Queues are plain deques shared by the whole process. A drain that has to
    wait registers a future on its own event loop, and publishing resolves that
    future through the loop it came from, so nothing shared binds to one loop.
    """

    # Shared state across all channels
    _queues: ClassVar[dict[str, deque[bytes]]] = {}
    _exchanges: ClassVar[dict[str, dict]] = {}
    _bindings: ClassVar[dict[str, list[tuple[str, str]]]] = defaultdict(list)
    #: Futures waiting for a message, keyed by queue name, each paired with the
    #: event loop it was created on.
    _waiters: ClassVar[dict[str, list[tuple[AbstractEventLoop, Future[None]]]]] = defaultdict(list)
    #: Guards the queues and the waiter registry against publishers running in
    #: another thread.
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._channel_id = str(uuid.uuid4())
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self._closed = False
        #: Consumer to start the next delivery at, so that one busy queue
        #: cannot starve the others.
        self._next_consumer = 0

        # For no-ack consumers
        self.no_ack_consumers: set[str] | None = set()

        # Unacked messages (delivery_tag -> (queue, message_data))
        self._unacked: dict[str, tuple[str, bytes]] = {}
        self._delivery_tag_counter = 0

    def _next_delivery_tag(self) -> str:
        """Generate next delivery tag."""
        self._delivery_tag_counter += 1
        return f"{self._channel_id}.{self._delivery_tag_counter}"

    # Shared queue primitives

    @classmethod
    def _get_queue(cls, name: str) -> deque[bytes]:
        """Get or create the deque backing the given queue."""
        with cls._lock:
            return cls._queues.setdefault(name, deque())

    @classmethod
    def _put(cls, name: str, data: bytes) -> None:
        """Append a message and wake everything waiting on that queue."""
        with cls._lock:
            cls._queues.setdefault(name, deque()).append(data)
            waiters = cls._waiters.pop(name, [])
        for loop, waiter in waiters:
            _wake(loop, waiter)

    @classmethod
    def _take(cls, name: str) -> bytes | None:
        """Pop the oldest message from a queue, or None if it is empty."""
        with cls._lock:
            queue = cls._queues.get(name)
            return queue.popleft() if queue else None

    @classmethod
    def _register_waiter(
        cls,
        names: Iterable[str],
        loop: AbstractEventLoop,
        waiter: Future[None],
    ) -> None:
        with cls._lock:
            for name in names:
                cls._waiters[name].append((loop, waiter))

    @classmethod
    def _discard_waiter(cls, names: Iterable[str], waiter: Future[None]) -> None:
        with cls._lock:
            for name in names:
                remaining = [entry for entry in cls._waiters.get(name, ()) if entry[1] is not waiter]
                if remaining:
                    cls._waiters[name] = remaining
                else:
                    cls._waiters.pop(name, None)

    async def close(self) -> None:
        """Close the channel."""
        if self._closed:
            return
        self._closed = True

        for queue, data in self._unacked.values():
            self._put(queue, data)
        self._unacked.clear()
        self._consumers.clear()

    # Exchange operations

    async def declare_exchange(self, exchange: Exchange) -> None:
        """Declare an exchange."""
        self._exchanges[exchange.name] = {
            "type": exchange.type,
            "durable": exchange.durable,
            "auto_delete": exchange.auto_delete,
            "arguments": exchange.arguments,
        }

    async def exchange_delete(self, exchange: str) -> None:
        """Delete an exchange."""
        self._exchanges.pop(exchange, None)
        self._bindings.pop(exchange, None)

    # Queue operations

    async def declare_queue(self, queue: Queue) -> str:
        """Declare a queue."""
        name = queue.name or f"amq.gen-{uuid.uuid4()}"
        queue.name = name

        # Create the queue
        self._get_queue(name)

        # Store binding if exchange is specified
        if queue.exchange:
            await self.queue_bind(
                queue=name,
                exchange=queue.exchange.name,
                routing_key=queue.routing_key,
            )
        return name

    async def queue_bind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Bind a queue to an exchange."""
        binding = (queue, routing_key or queue)
        if binding not in self._bindings[exchange]:
            self._bindings[exchange].append(binding)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Unbind a queue from an exchange."""
        binding = (queue, routing_key or queue)
        if binding in self._bindings[exchange]:
            self._bindings[exchange].remove(binding)

    async def queue_purge(self, queue: str) -> int:
        """Purge all messages from a queue."""
        with self._lock:
            messages = self._queues.get(queue)
            if not messages:
                return 0
            count = len(messages)
            messages.clear()
        return count

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete a queue."""
        with self._lock:
            messages = self._queues.get(queue)
            if messages is None:
                return 0
            if if_empty and messages:
                return 0
            count = len(messages)
            del self._queues[queue]

        # Remove from all exchange bindings
        for exchange in list(self._bindings.keys()):
            self._bindings[exchange] = [(q_name, rk) for q_name, rk in self._bindings[exchange] if q_name != queue]

        return count

    # Message operations

    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        """Publish a message to an exchange."""
        exchange = exchange or ""
        exchange_meta = self._exchanges.get(exchange, {"type": "direct"})
        exchange_type = exchange_meta.get("type", "direct")

        if exchange_type == "fanout":
            self._fanout_publish(exchange, message)
        elif exchange_type == "topic":
            self._topic_publish(exchange, routing_key, message)
        else:
            self._direct_publish(exchange, routing_key, message)

    def _direct_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to direct exchange."""
        if exchange and exchange in self._bindings:
            for queue, rk in self._bindings[exchange]:
                if rk == routing_key:
                    self._put(queue, message)
        else:
            # Default exchange: routing_key is the queue name
            self._put(routing_key, message)

    def _fanout_publish(self, exchange: str, message: bytes) -> None:
        """Publish to fanout exchange."""
        for queue, _ in self._bindings.get(exchange, ()):
            self._put(queue, message)

    def _topic_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to topic exchange with pattern matching."""
        for queue, pattern in self._bindings.get(exchange, ()):
            if self._topic_match(routing_key, pattern):
                self._put(queue, message)

    def _topic_match(self, routing_key: str, pattern: str) -> bool:
        """Match routing key against topic pattern.

        Supports:
        - * matches exactly one word
        - # matches zero or more words (including zero)
        """
        regex_pattern = pattern.replace(".", r"\.")
        regex_pattern = regex_pattern.replace("*", r"[^.]+")
        regex_pattern = regex_pattern.replace(r"\.#", r"(\..*)?")  # dot-hash: zero or more words
        regex_pattern = regex_pattern.replace(r"#\.", r"(.*\.)?")  # hash-dot: zero or more words
        regex_pattern = regex_pattern.replace("#", r".*")  # standalone hash
        regex_pattern = f"^{regex_pattern}$"
        return bool(re.match(regex_pattern, routing_key))

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        """Get a single message from a queue."""
        data = self._take(queue)
        if data is None:
            return None
        return self._create_message(queue, data, no_ack, accept)

    async def basic_consume(
        self,
        queue: str,
        callback: Callable[[Message], Any],
        consumer_tag: str | None = None,
        no_ack: bool = False,
    ) -> str:
        """Register a consumer for a queue."""
        if consumer_tag is None:
            consumer_tag = str(uuid.uuid4())

        self._consumers[consumer_tag] = (queue, callback, no_ack)

        if no_ack and self.no_ack_consumers is not None:
            self.no_ack_consumers.add(consumer_tag)

        return consumer_tag

    async def basic_cancel(self, consumer_tag: str) -> None:
        """Cancel a consumer."""
        self._consumers.pop(consumer_tag, None)
        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)

    async def drain_events(self, timeout: float | None = None) -> bool:
        """Deliver one queued message to its consumer.

        ``timeout=0`` polls: it delivers a message that is already queued and
        returns straight away. A positive timeout waits at most that long, and
        ``None`` waits indefinitely. Returns True if a message was delivered.

        Cancelling a call in progress delivers nothing and leaves every queued
        message where it is.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            names = {queue for queue, _, _ in self._consumers.values()}
            waiter: Future[None] = loop.create_future()
            # Register before reading the queues: a message published in
            # between then resolves the waiter instead of going unnoticed.
            self._register_waiter(names, loop, waiter)
            try:
                if await self._deliver_ready():
                    return True

                if deadline is None:
                    wait = MAX_WAIT
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return False
                    wait = min(remaining, MAX_WAIT)

                handle = loop.call_later(wait, _resolve, waiter)
                try:
                    await waiter
                finally:
                    handle.cancel()
            finally:
                self._discard_waiter(names, waiter)

    async def _deliver_ready(self) -> bool:
        """Deliver one queued message, taking the consumers in turn."""
        consumers = list(self._consumers.values())
        if not consumers:
            return False

        start = self._next_consumer % len(consumers)
        for offset in range(len(consumers)):
            index = (start + offset) % len(consumers)
            queue, callback, no_ack = consumers[index]
            data = self._take(queue)
            if data is None:
                continue
            self._next_consumer = index + 1
            message = self._create_message(queue, data, no_ack)
            await self._deliver_message(callback, message)
            return True
        return False

    async def _deliver_message(
        self,
        callback: Callable[..., Any],
        message: Message,
    ) -> None:
        """Deliver a message to a callback."""
        try:
            body = message.decode()
        except KombuError as exc:
            logger.warning("Cannot decode message %s: %r", message.delivery_tag, exc)
            body = message.body

        result = callback(body, message)
        if asyncio.iscoroutine(result):
            await result

    def _create_message(
        self,
        queue: str,
        data: bytes,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message:
        """Create a Message object from raw data."""
        envelope = decode_envelope(data, queue)
        delivery_tag = self._next_delivery_tag()

        if not no_ack:
            self._unacked[delivery_tag] = (queue, data)

        return Message(
            body=envelope.body,
            delivery_tag=delivery_tag,
            content_type=envelope.content_type,
            content_encoding=envelope.content_encoding,
            delivery_info={
                "exchange": "",
                "routing_key": queue,
            },
            properties=envelope.properties,
            headers=envelope.headers,
            accept=accept,
            channel=self,
        )

    # Acknowledgment operations

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        """Acknowledge a message."""
        if multiple:
            for tag in self._tags_up_to(delivery_tag):
                self._unacked.pop(tag, None)
        else:
            self._unacked.pop(delivery_tag, None)

    def _tags_up_to(self, delivery_tag: str) -> list[str]:
        """Return the unacked tags up to and including `delivery_tag`.

        Empty when the tag is not outstanding, so a stale multiple-ack cannot
        take the whole channel's unacked messages with it.
        """
        if delivery_tag not in self._unacked:
            return []
        tags = []
        for tag in self._unacked:
            tags.append(tag)
            if tag == delivery_tag:
                break
        return tags

    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        """Reject a message."""
        entry = self._unacked.pop(delivery_tag, None)
        if entry and requeue:
            queue, data = entry
            self._put(queue, data)

    async def basic_recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages."""
        if requeue:
            for queue, data in list(self._unacked.values()):
                self._put(queue, data)
        self._unacked.clear()


_Channel = Channel


class Transport(BaseTransport):
    """Pure asyncio in-memory transport.

    All channels in the process share one set of queues.
    """

    Channel = _Channel
    default_port = None

    driver_type = "memory"
    driver_name = "memory"

    def __init__(self, url: str = "memory://", **options: Any):
        super().__init__(url, **options)
        self._channels: list[Channel] = []
        self._connected = False

    async def connect(self) -> None:
        """Connect (no-op for memory transport)."""
        self._connected = True
        logger.debug("Memory transport connected")

    async def close(self) -> None:
        """Close the transport and all channels."""
        for channel in self._channels:
            await channel.close()
        self._channels.clear()
        self._connected = False

    async def create_channel(self) -> _Channel:
        """Create a new channel."""
        if not self._connected:
            await self.connect()

        channel = Channel()
        self._channels.append(channel)
        return channel

    @property
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        return self._connected

    def driver_version(self) -> str:
        """Return driver version."""
        return "1.0"

    @classmethod
    def reset_state(cls) -> None:
        """Drop every queue, exchange and binding in the process.

        Test hook: the queues outlive the connections that used them, so a
        suite that wants each test to start empty calls this between tests.
        """
        with Channel._lock:
            Channel._queues.clear()
            Channel._waiters.clear()
        Channel._exchanges.clear()
        Channel._bindings.clear()
