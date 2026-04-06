"""Pure asyncio file-system transport for Kombu.

Transport using the file-system as the message store. Messages written to the
queue are stored in `data_folder_in` directory and messages read from the
queue are read from `data_folder_out` directory.

Example:
    async with Connection('filesystem://', transport_options={
        'data_folder_in': 'data_in',
        'data_folder_out': 'data_out'
    }) as conn:
        async with conn.Producer() as producer:
            await producer.publish({'hello': 'world'}, routing_key='my_queue')

        async with conn.SimpleQueue('my_queue') as queue:
            message = await queue.get(timeout=5)
            print(message.payload)
            await message.ack()

Features
========
* Type: Filesystem
* Supports Direct: Yes
* Supports Topic: Yes
* Supports Fanout: Yes
* Supports Priority: No
* Supports TTL: No

Connection String
=================
.. code-block::

    filesystem://

Transport Options
=================
* ``data_folder_in`` - directory where messages are stored when written
  to queue (default: 'data_in').
* ``data_folder_out`` - directory from which messages are read when read from
  queue (default: 'data_out').
* ``store_processed`` - if set to True, all processed messages are backed up to
  ``processed_folder`` (default: False).
* ``processed_folder`` - directory where processed files are backed up
  (default: 'processed').
* ``control_folder`` - directory where exchange-queue bindings are stored
  (default: 'control').
"""

import asyncio
import base64
import re
import shutil
import tempfile
import uuid
from collections import namedtuple
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar

from kombu.exceptions import ChannelError
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

logger = get_logger("kombu.transport.filesystem")

VERSION = (1, 0, 0)
__version__ = ".".join(map(str, VERSION))

# Exchange-queue binding tuple
exchange_queue_t = namedtuple("exchange_queue_t", ["routing_key", "pattern", "queue"])


class Channel(BaseChannel):
    """Pure asyncio filesystem channel.

    Uses asyncio.to_thread() for non-blocking file I/O operations.
    """

    # Shared state across all channels
    _exchanges: ClassVar[dict[str, dict]] = {}
    _bindings: ClassVar[dict[str, list[exchange_queue_t]]] = {}

    def __init__(
        self,
        transport: Transport,
        connection_id: str,
        data_folder_in: str = "data_in",
        data_folder_out: str = "data_out",
        store_processed: bool = False,
        processed_folder: str = "processed",
        control_folder: str = "control",
    ):
        self._transport = transport
        self._connection_id = connection_id
        self._channel_id = str(uuid.uuid4())
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self._closed = False

        # Filesystem options
        self._data_folder_in = Path(data_folder_in)
        self._data_folder_out = Path(data_folder_out)
        self._store_processed = store_processed
        self._processed_folder = Path(processed_folder)
        self._control_folder = Path(control_folder)

        # For no-ack consumers
        self.no_ack_consumers: set[str] | None = set()

        # Unacked messages (delivery_tag -> (queue, filepath))
        self._unacked: dict[str, tuple[str, Path]] = {}
        self._delivery_tag_counter = 0

    def _next_delivery_tag(self) -> str:
        """Generate next delivery tag."""
        self._delivery_tag_counter += 1
        return f"{self._channel_id}.{self._delivery_tag_counter}"

    async def _ensure_directories(self) -> None:
        """Ensure all required directories exist."""
        for folder in [
            self._data_folder_in,
            self._data_folder_out,
            self._control_folder,
        ]:
            folder.mkdir(parents=True, exist_ok=True)

        if self._store_processed:
            self._processed_folder.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        """Close the channel."""
        if self._closed:
            return
        self._closed = True

        # Requeue unacked messages by moving files back
        for delivery_tag, (queue, filepath) in self._unacked.items():
            try:
                if filepath.exists():
                    # Move back to data_folder_in for reprocessing
                    dest = self._data_folder_in / filepath.name
                    shutil.move(str(filepath), str(dest))
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

        # Delete exchange file
        exchange_file = self._control_folder / f"{exchange}.exchange"
        if exchange_file.exists():
            exchange_file.unlink()

    # Queue operations

    async def declare_queue(self, queue: Queue) -> str:
        """Declare a queue."""
        await self._ensure_directories()

        name = queue.name or f"amq.gen-{uuid.uuid4()}"
        queue.name = name

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
        await self._ensure_directories()

        binding = exchange_queue_t(routing_key or "", "", queue)

        # Update in-memory bindings
        if exchange not in self._bindings:
            self._bindings[exchange] = []
        if binding not in self._bindings[exchange]:
            self._bindings[exchange].append(binding)

        # Persist to control folder
        await self._save_exchange_bindings(exchange)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Unbind a queue from an exchange."""
        binding = exchange_queue_t(routing_key or "", "", queue)
        if exchange in self._bindings and binding in self._bindings[exchange]:
            self._bindings[exchange].remove(binding)
            await self._save_exchange_bindings(exchange)

    async def _save_exchange_bindings(self, exchange: str) -> None:
        """Save exchange bindings to control folder."""
        exchange_file = self._control_folder / f"{exchange}.exchange"
        bindings = self._bindings.get(exchange, [])
        data = json_dumps([list(b) for b in bindings])

        def _write():
            with exchange_file.open("w") as f:
                f.write(data)

        await asyncio.to_thread(_write)

    async def _load_exchange_bindings(self, exchange: str) -> list[exchange_queue_t]:
        """Load exchange bindings from control folder."""
        if exchange in self._bindings:
            return self._bindings[exchange]

        exchange_file = self._control_folder / f"{exchange}.exchange"
        if not exchange_file.exists():
            return []

        def _read():
            with exchange_file.open() as f:
                return f.read()

        try:
            data = await asyncio.to_thread(_read)
            bindings = json_loads(data)
            self._bindings[exchange] = [exchange_queue_t(*b) for b in bindings]
            return self._bindings[exchange]
        except Exception:
            return []

    async def queue_purge(self, queue: str) -> int:
        """Purge all messages from a queue."""
        count = 0
        queue_pattern = f".{queue}.msg"

        try:
            for entry in self._data_folder_in.iterdir():
                if queue_pattern in entry.name:
                    try:
                        entry.unlink()
                        count += 1
                    except OSError:
                        pass
        except FileNotFoundError:
            pass

        return count

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete a queue."""
        if if_empty:
            size = await self._queue_size(queue)
            if size > 0:
                return 0

        count = await self.queue_purge(queue)

        # Remove from all exchange bindings
        for exchange in list(self._bindings.keys()):
            self._bindings[exchange] = [b for b in self._bindings[exchange] if b.queue != queue]
            await self._save_exchange_bindings(exchange)

        return count

    async def _queue_size(self, queue: str) -> int:
        """Return the number of messages in a queue."""
        count = 0
        queue_pattern = f".{queue}.msg"

        try:
            for entry in self._data_folder_in.iterdir():
                if queue_pattern in entry.name:
                    count += 1
        except FileNotFoundError:
            pass

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
        await self._ensure_directories()

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
        if exchange:
            bindings = await self._load_exchange_bindings(exchange)
            for binding in bindings:
                if binding.routing_key == routing_key:
                    await self._put_message(binding.queue, message)
        else:
            # Default exchange: routing_key is the queue name
            await self._put_message(routing_key, message)

    async def _fanout_publish(
        self,
        exchange: str,
        message: bytes,
    ) -> None:
        """Publish to fanout exchange."""
        bindings = await self._load_exchange_bindings(exchange)
        for binding in bindings:
            await self._put_message(binding.queue, message)

    async def _topic_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to topic exchange with pattern matching."""
        bindings = await self._load_exchange_bindings(exchange)
        for binding in bindings:
            if self._topic_match(routing_key, binding.routing_key):
                await self._put_message(binding.queue, message)

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

    async def _put_message(self, queue: str, message: bytes) -> None:
        """Write a message to the filesystem."""
        timestamp = round(monotonic() * 1000)
        filename = f"{timestamp}_{uuid.uuid4()}.{queue}.msg"
        filepath = self._data_folder_out / filename

        def _write():
            with filepath.open("wb") as f:
                f.write(message)

        try:
            await asyncio.to_thread(_write)
        except OSError as e:
            raise ChannelError(f"Cannot write message to {filepath}: {e}") from e

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: set[str] | None = None,
    ) -> Message | None:
        """Get a single message from a queue."""
        queue_pattern = f".{queue}.msg"

        try:
            files = sorted(entry.name for entry in self._data_folder_in.iterdir())
        except FileNotFoundError:
            return None

        for filename in files:
            if queue_pattern not in filename:
                continue

            src_path = self._data_folder_in / filename

            # Determine destination for processed file
            if self._store_processed:
                dest_folder = self._processed_folder
            else:
                dest_folder = Path(tempfile.gettempdir())

            dest_path = dest_folder / filename

            try:
                # Move file to prevent other workers from processing it
                shutil.move(str(src_path), str(dest_path))
            except OSError:
                # File may be locked or already moved
                continue

            def _read(path=dest_path):
                with path.open("rb") as f:
                    return f.read()

            try:
                data = await asyncio.to_thread(_read)

                if not self._store_processed:
                    dest_path.unlink()
                    processed_path = None
                else:
                    processed_path = dest_path

                return self._create_message(queue, data, no_ack, accept, processed_path)
            except OSError as e:
                raise ChannelError(f"Cannot read message from {dest_path}: {e}") from e

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
            await asyncio.sleep(0.1)
            return False

        # Check all consumer queues
        for queue, callback, no_ack in self._consumers.values():
            message = await self.get(queue, no_ack=no_ack)
            if message:
                await self._deliver_message(callback, message)
                return True

        # No messages available, wait a bit
        effective_timeout = min(timeout or 1.0, 1.0)
        await asyncio.sleep(effective_timeout)
        return False

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
        filepath: Path | None = None,
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

        if not no_ack and filepath:
            self._unacked[delivery_tag] = (queue, filepath)

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
                entry = self._unacked.pop(tag, None)
                if entry and not self._store_processed:
                    _, filepath = entry
                    if filepath and filepath.exists():
                        filepath.unlink()
        else:
            entry = self._unacked.pop(delivery_tag, None)
            if entry and not self._store_processed:
                _, filepath = entry
                if filepath and filepath.exists():
                    filepath.unlink()

    async def basic_reject(self, delivery_tag: str, requeue: bool = True) -> None:
        """Reject a message."""
        entry = self._unacked.pop(delivery_tag, None)
        if entry:
            queue, filepath = entry
            if requeue and filepath and filepath.exists():
                # Move back to data_folder_in
                dest = self._data_folder_in / filepath.name
                shutil.move(str(filepath), str(dest))
            elif filepath and filepath.exists():
                filepath.unlink()

    async def basic_recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages."""
        for _delivery_tag, (_queue, filepath) in list(self._unacked.items()):
            if requeue and filepath and filepath.exists():
                dest = self._data_folder_in / filepath.name
                shutil.move(str(filepath), str(dest))
            elif filepath and filepath.exists():
                filepath.unlink()
        self._unacked.clear()


class Transport(BaseTransport):
    """Pure asyncio filesystem transport.

    Uses the filesystem for message storage.
    """

    Channel = Channel
    default_port = None

    driver_type = "filesystem"
    driver_name = "filesystem"

    def __init__(self, url: str = "filesystem://", **options: Any):
        super().__init__(url, **options)
        self._channels: list[Channel] = []
        self._connection_id = str(uuid.uuid4())
        self._connected = False

        # Extract transport options
        self._data_folder_in = options.get("data_folder_in", "data_in")
        self._data_folder_out = options.get("data_folder_out", "data_out")
        self._store_processed = options.get("store_processed", False)
        self._processed_folder = options.get("processed_folder", "processed")
        self._control_folder = options.get("control_folder", "control")

    async def connect(self) -> None:
        """Connect (ensures directories exist)."""
        # Ensure directories exist
        for folder in [self._data_folder_in, self._data_folder_out, self._control_folder]:
            Path(folder).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

        if self._store_processed:
            Path(self._processed_folder).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

        self._connected = True
        logger.debug("Filesystem transport connected")

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

        channel = Channel(
            self,
            self._connection_id,
            data_folder_in=self._data_folder_in,
            data_folder_out=self._data_folder_out,
            store_processed=self._store_processed,
            processed_folder=self._processed_folder,
            control_folder=self._control_folder,
        )
        self._channels.append(channel)
        return channel

    @property
    def is_connected(self) -> bool:
        """Check if transport is connected."""
        return self._connected

    def driver_version(self) -> str:
        """Return driver version."""
        return __version__

    @classmethod
    def reset_state(cls) -> None:
        """Reset all shared state (useful for testing)."""
        Channel._exchanges.clear()
        Channel._bindings.clear()
