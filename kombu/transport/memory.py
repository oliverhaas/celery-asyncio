"""Pure asyncio in-memory transport for Kombu.

Simple transport using asyncio.Queue for storing messages.
Messages can be passed between coroutines within the same process.

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
import base64
import re
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any, ClassVar

from kombu.log import get_logger
from kombu.message import Message
from kombu.utils.json import dumps as json_dumps
from kombu.utils.json import loads as json_loads

from .base import Channel as BaseChannel
from .base import Transport as BaseTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    from kombu.entity import Exchange, Queue

__all__ = ("Channel", "Transport")

logger = get_logger("kombu.transport.memory")


class Channel(BaseChannel):
    """Pure asyncio in-memory channel.

    Uses asyncio.Queue for message storage.
    """

    # Shared state across all channels
    _queues: ClassVar[dict[str, asyncio.Queue]] = {}
    _exchanges: ClassVar[dict[str, dict]] = {}
    _bindings: ClassVar[dict[str, list[tuple[str, str]]]] = defaultdict(list)

    def __init__(self, transport: Transport, connection_id: str):
        self._transport = transport
        self._connection_id = connection_id
        self._channel_id = str(uuid.uuid4())
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self._closed = False

        # For no-ack consumers
        self.no_ack_consumers: set[str] | None = set()

        # Unacked messages (delivery_tag -> (queue, message_data))
        self._unacked: dict[str, tuple[str, bytes]] = {}
        self._delivery_tag_counter = 0

    def _next_delivery_tag(self) -> str:
        """Generate next delivery tag."""
        self._delivery_tag_counter += 1
        return f"{self._channel_id}.{self._delivery_tag_counter}"

    def _get_queue(self, name: str) -> asyncio.Queue:
        """Get or create an asyncio.Queue for the given name."""
        if name not in self._queues:
            self._queues[name] = asyncio.Queue()
        return self._queues[name]

    async def close(self) -> None:
        """Close the channel."""
        if self._closed:
            return
        self._closed = True

        # Requeue unacked messages
        for delivery_tag, (queue, data) in self._unacked.items():
            try:
                await self._get_queue(queue).put(data)
            except Exception:
                logger.warning(
                    "Failed to requeue message %s to %s",
                    delivery_tag,
                    queue,
                )
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
        q = self._get_queue(queue)
        count = q.qsize()
        # Clear the queue
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        return count

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete a queue."""
        if queue not in self._queues:
            return 0

        q = self._queues[queue]

        if if_empty and not q.empty():
            return 0

        count = q.qsize()
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
            await self._fanout_publish(exchange, message)
        elif exchange_type == "topic":
            await self._topic_publish(exchange, routing_key, message)
        else:
            await self._direct_publish(exchange, routing_key, message)

    async def _direct_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to direct exchange."""
        if exchange and exchange in self._bindings:
            for queue, rk in self._bindings[exchange]:
                if rk == routing_key:
                    await self._get_queue(queue).put(message)
        else:
            # Default exchange: routing_key is the queue name
            await self._get_queue(routing_key).put(message)

    async def _fanout_publish(
        self,
        exchange: str,
        message: bytes,
    ) -> None:
        """Publish to fanout exchange."""
        if exchange in self._bindings:
            for queue, _ in self._bindings[exchange]:
                await self._get_queue(queue).put(message)

    async def _topic_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to topic exchange with pattern matching."""
        if exchange not in self._bindings:
            return

        for queue, pattern in self._bindings[exchange]:
            if self._topic_match(routing_key, pattern):
                await self._get_queue(queue).put(message)

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
        accept: set[str] | None = None,
    ) -> Message | None:
        """Get a single message from a queue."""
        q = self._get_queue(queue)
        try:
            data = q.get_nowait()
            return self._create_message(queue, data, no_ack, accept)
        except asyncio.QueueEmpty:
            return None

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
        """Wait for and deliver messages to consumers."""
        if not self._consumers:
            await asyncio.sleep(0.01)
            return False

        # Check all consumer queues
        for queue, callback, no_ack in self._consumers.values():
            q = self._get_queue(queue)
            try:
                # Try non-blocking first
                data = q.get_nowait()
                message = self._create_message(queue, data, no_ack)
                await self._deliver_message(callback, message)
                return True
            except asyncio.QueueEmpty:
                continue

        # No messages available, wait with timeout
        effective_timeout = timeout or 1.0
        # Capture consumer list once — dict could change during await below
        consumer_list = list(self._consumers.values())
        queues = [self._get_queue(q) for q, _, _ in consumer_list]

        # Create tasks to wait on all queues
        wait_tasks = [asyncio.create_task(q.get()) for q in queues]

        try:
            done, pending = await asyncio.wait(
                wait_tasks,
                timeout=effective_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if done:
                delivered = False
                # Put back results from extra completed tasks, deliver only the first
                for i, task in enumerate(wait_tasks):
                    if task in done:
                        data = task.result()
                        if i < len(consumer_list):
                            if not delivered:
                                queue, callback, no_ack = consumer_list[i]
                                message = self._create_message(queue, data, no_ack)
                                await self._deliver_message(callback, message)
                                delivered = True
                            else:
                                # Put the message back — it was consumed from the asyncio.Queue
                                # but we can only deliver one per drain_events call
                                q_name = consumer_list[i][0]
                                await self._get_queue(q_name).put(data)
                return delivered
        except Exception:
            # Cancel all tasks on error
            for task in wait_tasks:
                task.cancel()
            raise

    async def _deliver_message(
        self,
        callback: Callable[[Message], Any],
        message: Message,
    ) -> None:
        """Deliver a message to a callback."""
        try:
            body = message.decode()
        except Exception:
            body = message.body

        result = callback(body, message)
        if asyncio.iscoroutine(result):
            await result

    def _create_message(
        self,
        queue: str,
        data: bytes,
        no_ack: bool = False,
        accept: set[str] | None = None,
    ) -> Message:
        """Create a Message object from raw data."""
        try:
            payload = json_loads(data)
            body = payload.get("body", data)
            content_type = payload.get("content-type", "application/json")
            content_encoding = payload.get("content-encoding", "utf-8")
            properties = payload.get("properties", {})
            headers = payload.get("headers", {})

            if isinstance(body, str):
                if headers.get("body_encoding") == "base64":
                    body = base64.b64decode(body)
                elif content_encoding not in ("binary", "ascii-8bit"):
                    body = body.encode(content_encoding)
                else:
                    body = body.encode("utf-8")
            elif isinstance(body, dict | list):
                body = json_dumps(body).encode("utf-8")
        except ValueError, TypeError:  # fmt: skip
            body = data
            content_type = "application/data"
            content_encoding = "binary"
            properties = {}
            headers = {}

        delivery_tag = self._next_delivery_tag()

        if not no_ack:
            self._unacked[delivery_tag] = (queue, data)

        return Message(
            body=body,
            delivery_tag=delivery_tag,
            content_type=content_type,
            content_encoding=content_encoding,
            delivery_info={
                "exchange": "",
                "routing_key": queue,
            },
            properties=properties,
            headers=headers,
            accept=accept,
            channel=self,
        )

    # Acknowledgment operations

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        """Acknowledge a message."""
        if multiple:
            tags_to_ack = []
            for tag in self._unacked:
                tags_to_ack.append(tag)
                if tag == delivery_tag:
                    break
            for tag in tags_to_ack:
                self._unacked.pop(tag, None)
        else:
            self._unacked.pop(delivery_tag, None)

    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        """Reject a message."""
        entry = self._unacked.pop(delivery_tag, None)
        if entry and requeue:
            queue, data = entry
            await self._get_queue(queue).put(data)

    async def basic_recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages."""
        if requeue:
            for _delivery_tag, (queue, data) in list(self._unacked.items()):
                await self._get_queue(queue).put(data)
        self._unacked.clear()


class Transport(BaseTransport):
    """Pure asyncio in-memory transport.

    Uses asyncio.Queue for message storage within a single process.
    """

    Channel = Channel
    default_port = None

    driver_type = "memory"
    driver_name = "memory"

    def __init__(self, url: str = "memory://", **options: Any):
        super().__init__(url, **options)
        self._channels: list[Channel] = []
        self._connection_id = str(uuid.uuid4())
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

    async def create_channel(self) -> Channel:
        """Create a new channel."""
        if not self._connected:
            await self.connect()

        channel = Channel(self, self._connection_id)
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
        """Reset all shared state (useful for testing)."""
        Channel._queues.clear()
        Channel._exchanges.clear()
        Channel._bindings.clear()
