"""Mock transport for testing kombu-asyncio.

Provides async MockChannel and MockTransport that implement
the base ABC from kombu.transport.base, useful for unit testing
without requiring a real broker.
"""

import asyncio
import re
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

from kombu.message import Message
from kombu.transport.base import Channel as BaseChannel
from kombu.transport.base import Transport as BaseTransport
from kombu.utils.json import loads as json_loads

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue

__all__ = ("ContextMock", "MockChannel", "MockTransport")


class _ContextMock(Mock):
    """Mock with async context manager support."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def ContextMock(*args, **kwargs):
    """Mock that supports both sync and async context managers."""
    obj = _ContextMock(*args, **kwargs)
    obj.__aenter__ = _ContextMock(return_value=obj)
    obj.__aexit__ = _ContextMock(return_value=None)
    return obj


class MockChannel(BaseChannel):
    """Async mock channel for testing.

    Implements the Channel ABC with in-memory storage.
    Tracks all calls for assertion in tests.
    """

    def __init__(self, transport: MockTransport | None = None):
        self._transport = transport
        self._closed = False
        self._channel_id = str(uuid.uuid4())[:8]

        # In-memory state
        self._exchanges: dict[str, dict] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._bindings: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self._unacked: dict[str, tuple[str, bytes]] = {}
        self._delivery_tag_counter = 0

        # For no-ack tracking
        self.no_ack_consumers: set[str] | None = set()

        # Call tracking
        self.calls: list[tuple[str, tuple, dict]] = []

    def _track(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    def _next_delivery_tag(self) -> str:
        self._delivery_tag_counter += 1
        return f"{self._channel_id}.{self._delivery_tag_counter}"

    def _get_queue(self, name: str) -> asyncio.Queue:
        if name not in self._queues:
            self._queues[name] = asyncio.Queue()
        return self._queues[name]

    async def close(self) -> None:
        self._track("close")
        if self._closed:
            return
        self._closed = True
        # Requeue unacked
        for queue_name, data in self._unacked.values():
            await self._get_queue(queue_name).put(data)
        self._unacked.clear()
        self._consumers.clear()

    async def declare_exchange(self, exchange: Exchange) -> None:
        self._track("declare_exchange", exchange)
        self._exchanges[exchange.name] = {
            "type": exchange.type,
            "durable": exchange.durable,
            "auto_delete": exchange.auto_delete,
        }

    async def exchange_delete(self, exchange: str) -> None:
        self._track("exchange_delete", exchange)
        self._exchanges.pop(exchange, None)
        self._bindings.pop(exchange, None)

    async def declare_queue(self, queue: Queue) -> str:
        self._track("declare_queue", queue)
        name = queue.name or f"amq.gen-{uuid.uuid4().hex[:8]}"
        self._get_queue(name)
        return name

    async def queue_bind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        self._track("queue_bind", queue, exchange, routing_key)
        binding = (queue, routing_key)
        if binding not in self._bindings[exchange]:
            self._bindings[exchange].append(binding)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        self._track("queue_unbind", queue, exchange, routing_key)
        binding = (queue, routing_key)
        try:
            self._bindings[exchange].remove(binding)
        except ValueError:
            pass

    async def queue_purge(self, queue: str) -> int:
        self._track("queue_purge", queue)
        q = self._get_queue(queue)
        count = q.qsize()
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
        self._track("queue_delete", queue)
        q = self._queues.pop(queue, None)
        count = q.qsize() if q else 0
        # Remove bindings
        for exchange_bindings in self._bindings.values():
            exchange_bindings[:] = [(q_name, rk) for q_name, rk in exchange_bindings if q_name != queue]
        return count

    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        self._track("publish", message, exchange, routing_key)
        exchange_meta = self._exchanges.get(exchange, {"type": "direct"})
        exchange_type = exchange_meta.get("type", "direct")

        if exchange_type == "fanout":
            for q_name, _rk in self._bindings.get(exchange, []):
                await self._get_queue(q_name).put(message)
        elif exchange_type == "topic":
            for q_name, pattern in self._bindings.get(exchange, []):
                if _topic_match(pattern, routing_key):
                    await self._get_queue(q_name).put(message)
        # Direct: use routing_key as queue name, or check bindings
        elif self._bindings.get(exchange):
            for q_name, rk in self._bindings[exchange]:
                if rk == routing_key:
                    await self._get_queue(q_name).put(message)
        else:
            await self._get_queue(routing_key).put(message)

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        self._track("get", queue)
        q = self._get_queue(queue)
        try:
            data = q.get_nowait()
        except asyncio.QueueEmpty:
            return None

        delivery_tag = self._next_delivery_tag()
        return self._make_message(data, delivery_tag, queue, "", no_ack)

    def _make_message(
        self,
        data: Any,
        delivery_tag: str,
        queue: str,
        consumer_tag: str,
        no_ack: bool,
    ) -> Message:
        """Build a Message from raw queue data."""
        msg_data = json_loads(data) if isinstance(data, (str, bytes)) else data

        if isinstance(msg_data, dict) and "body" in msg_data:
            body = msg_data["body"]
            content_type = msg_data.get("content-type", "application/json")
            content_encoding = msg_data.get("content-encoding", "utf-8")
            headers = msg_data.get("headers", {})
            properties = msg_data.get("properties", {})
        else:
            body = data
            content_type = "application/json"
            content_encoding = "utf-8"
            headers = {}
            properties = {}

        message = Message(
            body=body,
            delivery_tag=delivery_tag,
            content_type=content_type,
            content_encoding=content_encoding,
            headers=headers,
            properties=properties,
            delivery_info={
                "exchange": "",
                "routing_key": queue,
                "consumer_tag": consumer_tag,
            },
            channel=self,
        )

        if not no_ack:
            self._unacked[delivery_tag] = (queue, data)

        return message

    async def basic_consume(
        self,
        queue: str,
        callback: Callable[[Message], Any],
        consumer_tag: str | None = None,
        no_ack: bool = False,
    ) -> str:
        tag = consumer_tag or f"ctag.{uuid.uuid4().hex[:8]}"
        self._track("basic_consume", queue, tag)
        self._consumers[tag] = (queue, callback, no_ack)
        if no_ack and self.no_ack_consumers is not None:
            self.no_ack_consumers.add(tag)
        return tag

    async def basic_cancel(self, consumer_tag: str) -> None:
        self._track("basic_cancel", consumer_tag)
        self._consumers.pop(consumer_tag, None)
        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)

    async def drain_events(self, timeout: float | None = None) -> bool:
        self._track("drain_events", timeout)
        if not self._consumers:
            return False

        for tag, (queue_name, callback, no_ack) in list(self._consumers.items()):
            q = self._get_queue(queue_name)
            try:
                if timeout is not None and timeout > 0:
                    data = await asyncio.wait_for(q.get(), timeout=timeout)
                else:
                    data = q.get_nowait()
            except asyncio.QueueEmpty, TimeoutError:
                continue

            delivery_tag = self._next_delivery_tag()
            message = self._make_message(data, delivery_tag, queue_name, tag, no_ack)

            decoded = message.decode()
            callback(decoded, message)
            return True

        return False

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        self._track("basic_ack", delivery_tag)
        self._unacked.pop(delivery_tag, None)

    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        self._track("basic_reject", delivery_tag, requeue)
        entry = self._unacked.pop(delivery_tag, None)
        if requeue and entry:
            queue_name, data = entry
            await self._get_queue(queue_name).put(data)

    async def basic_recover(self, requeue: bool = True) -> None:
        self._track("basic_recover", requeue)
        if not requeue:
            return
        for queue_name, data in self._unacked.values():
            await self._get_queue(queue_name).put(data)
        self._unacked.clear()


class MockTransport(BaseTransport):
    """Async mock transport for testing."""

    Channel = MockChannel
    driver_type = "mock"
    driver_name = "mock"
    default_port = 0

    def __init__(self, url: str = "mock://", **options: Any):
        super().__init__(url, **options)
        self._connected = False
        self._channels: list[MockChannel] = []
        self.calls: list[tuple[str, tuple, dict]] = []

    def _track(self, method: str, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    async def connect(self) -> None:
        self._track("connect")
        self._connected = True

    async def close(self) -> None:
        self._track("close")
        for ch in self._channels:
            await ch.close()
        self._channels.clear()
        self._connected = False

    async def create_channel(self) -> MockChannel:
        self._track("create_channel")
        ch = MockChannel(transport=self)
        self._channels.append(ch)
        return ch

    @property
    def is_connected(self) -> bool:
        return self._connected

    def driver_version(self) -> str:
        return "mock-1.0"


def _topic_match(pattern: str, routing_key: str) -> bool:
    """Match AMQP-style topic patterns."""
    pattern_re = pattern.replace(".", r"\.").replace("*", r"[^.]+").replace("#", r".*")
    return bool(re.match(f"^{pattern_re}$", routing_key))
