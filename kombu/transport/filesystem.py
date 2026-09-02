"""Pure asyncio file-system transport for Kombu.

Transport using the file-system as the message store. Messages are written to
the ``data_folder_out`` directory and read from the ``data_folder_in``
directory; point both at the same directory to publish and consume through one
queue, or cross them over between two processes.

Example:
    async with Connection('filesystem://', transport_options={
        'data_folder_in': 'queue',
        'data_folder_out': 'queue'
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
* ``data_folder_in`` - directory messages are read from (default: 'data_in').
  Messages a consumer is working on are held in its ``inflight`` subdirectory
  until they are acknowledged.
* ``data_folder_out`` - directory messages are written to (default: 'data_out').
* ``store_processed`` - if set to True, acknowledged messages are moved to
  ``processed_folder`` instead of being deleted (default: False).
* ``processed_folder`` - directory where processed files are kept
  (default: 'processed').
* ``control_folder`` - directory where exchange-queue bindings are stored
  (default: 'control').
"""

import asyncio
import fcntl
import os
import re
import tempfile
import uuid
from collections import namedtuple
from pathlib import Path
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, ClassVar

from kombu.exceptions import ChannelError, KombuError
from kombu.log import get_logger
from kombu.message import Message
from kombu.utils.json import dumps as json_dumps
from kombu.utils.json import loads as json_loads

from .base import Channel as BaseChannel
from .base import Transport as BaseTransport
from .base import decode_envelope

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue

__all__ = ("Channel", "Transport")

logger = get_logger("kombu.transport.filesystem")

VERSION = (1, 0, 0)
__version__ = ".".join(map(str, VERSION))

#: Suffix of a message file. The name in front of it is
#: ``<timestamp>_<uuid>.<queue>``, which orders the queue by publish time.
MESSAGE_SUFFIX = ".msg"

#: Suffix of the file holding an exchange's bindings.
CONTROL_SUFFIX = ".exchange"

#: Subdirectory of ``data_folder_in`` holding messages that have been handed to
#: a consumer but not acknowledged yet.
INFLIGHT_DIR = "inflight"

#: How often drain_events looks for new files while waiting. Nothing tells the
#: transport a file arrived, so waiting means polling.
POLL_INTERVAL = 0.05

# Exchange-queue binding tuple
exchange_queue_t = namedtuple("exchange_queue_t", ["routing_key", "queue"])


def _queue_of(filename: str) -> str | None:
    """Name of the queue a message file belongs to, None if it is not one."""
    _, _, rest = filename.partition(".")
    if not rest.endswith(MESSAGE_SUFFIX):
        return None
    return rest[: -len(MESSAGE_SUFFIX)]


class Channel(BaseChannel):
    """Pure asyncio filesystem channel.

    Uses asyncio.to_thread() for non-blocking file I/O operations.
    """

    # Shared state across all channels
    _exchanges: ClassVar[dict[str, dict]] = {}

    def __init__(
        self,
        data_folder_in: str = "data_in",
        data_folder_out: str = "data_out",
        store_processed: bool = False,
        processed_folder: str = "processed",
        control_folder: str = "control",
    ):
        self._channel_id = str(uuid.uuid4())
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self._closed = False
        #: Consumer to start the next delivery at, so that one busy queue
        #: cannot starve the others.
        self._next_consumer = 0

        # Filesystem options
        self._data_folder_in = Path(data_folder_in)
        self._data_folder_out = Path(data_folder_out)
        self._inflight_folder = self._data_folder_in / INFLIGHT_DIR
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

    def _make_directories(self) -> None:
        for folder in (
            self._data_folder_in,
            self._data_folder_out,
            self._inflight_folder,
            self._control_folder,
        ):
            folder.mkdir(parents=True, exist_ok=True)

        if self._store_processed:
            self._processed_folder.mkdir(parents=True, exist_ok=True)

    async def _ensure_directories(self) -> None:
        """Ensure all required directories exist."""
        await asyncio.to_thread(self._make_directories)

    async def close(self) -> None:
        """Close the channel, releasing every message it still holds."""
        if self._closed:
            return
        self._closed = True

        unacked = list(self._unacked.items())
        self._unacked.clear()
        self._consumers.clear()
        for delivery_tag, (queue, filepath) in unacked:
            try:
                await asyncio.to_thread(self._move, filepath, self._data_folder_in)
            except OSError as exc:
                logger.warning(
                    "Cannot requeue message %s of queue %r: %r",
                    delivery_tag,
                    queue,
                    exc,
                )

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
        await asyncio.to_thread(self._delete_control_files, exchange)

    # The control file is the only record of a binding, so other workers can see it.

    def _control_path(self, exchange: str) -> Path:
        return self._control_folder / f"{exchange}{CONTROL_SUFFIX}"

    def _control_lock_path(self, exchange: str) -> Path:
        return self._control_folder / f"{exchange}{CONTROL_SUFFIX}.lock"

    def _delete_control_files(self, exchange: str) -> None:
        self._control_path(exchange).unlink(missing_ok=True)
        self._control_lock_path(exchange).unlink(missing_ok=True)

    def _read_bindings(self, exchange: str) -> list[exchange_queue_t]:
        """Read an exchange's bindings, raising if the control file is broken."""
        path = self._control_path(exchange)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return []
        if not raw:
            return []
        try:
            return [exchange_queue_t(*entry) for entry in json_loads(raw)]
        except (ValueError, TypeError) as exc:
            raise ChannelError(f"Cannot read the bindings of exchange {exchange!r} from {path}: {exc}") from exc

    def _write_bindings(self, exchange: str, bindings: list[exchange_queue_t]) -> None:
        """Replace the control file with one holding `bindings`."""
        path = self._control_path(exchange)
        handle, name = tempfile.mkstemp(dir=str(self._control_folder), prefix=path.name, suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w") as f:
                f.write(json_dumps([list(binding) for binding in bindings]))
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _update_bindings(
        self,
        exchange: str,
        change: Callable[[list[exchange_queue_t]], list[exchange_queue_t]],
    ) -> None:
        """Apply `change` to an exchange's bindings, holding its lock."""
        self._control_folder.mkdir(parents=True, exist_ok=True)
        with self._control_lock_path(exchange).open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            bindings = self._read_bindings(exchange)
            updated = change(bindings)
            if updated != bindings:
                self._write_bindings(exchange, updated)

    def _locked_read_bindings(self, exchange: str) -> list[exchange_queue_t]:
        lock_path = self._control_lock_path(exchange)
        if not lock_path.exists():
            return self._read_bindings(exchange)
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._read_bindings(exchange)

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
        binding = exchange_queue_t(routing_key or "", queue)

        def add(bindings: list[exchange_queue_t]) -> list[exchange_queue_t]:
            return bindings if binding in bindings else [*bindings, binding]

        await asyncio.to_thread(self._update_bindings, exchange, add)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        """Unbind a queue from an exchange."""
        binding = exchange_queue_t(routing_key or "", queue)

        def remove(bindings: list[exchange_queue_t]) -> list[exchange_queue_t]:
            return [b for b in bindings if b != binding]

        await asyncio.to_thread(self._update_bindings, exchange, remove)

    async def _load_exchange_bindings(self, exchange: str) -> list[exchange_queue_t]:
        """Load exchange bindings from control folder."""
        return await asyncio.to_thread(self._locked_read_bindings, exchange)

    def _known_exchanges(self) -> list[str]:
        """Names of the exchanges the control folder has bindings for."""
        try:
            entries = list(self._control_folder.iterdir())
        except FileNotFoundError:
            return []
        return [entry.name[: -len(CONTROL_SUFFIX)] for entry in entries if entry.name.endswith(CONTROL_SUFFIX)]

    async def queue_purge(self, queue: str) -> int:
        """Purge all messages from a queue."""
        return await asyncio.to_thread(self._purge, queue)

    def _purge(self, queue: str) -> int:
        count = 0
        for path in self._queued_files(queue):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            count += 1
        return count

    def _queued_files(self, queue: str) -> list[Path]:
        """Message files waiting in `queue`, oldest first."""
        try:
            names = sorted(entry.name for entry in self._data_folder_in.iterdir())
        except FileNotFoundError:
            return []
        return [self._data_folder_in / name for name in names if _queue_of(name) == queue]

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        """Delete a queue."""
        if if_empty and await self._queue_size(queue) > 0:
            return 0

        count = await self.queue_purge(queue)

        def drop(bindings: list[exchange_queue_t]) -> list[exchange_queue_t]:
            return [b for b in bindings if b.queue != queue]

        for exchange in await asyncio.to_thread(self._known_exchanges):
            await asyncio.to_thread(self._update_bindings, exchange, drop)

        return count

    async def _queue_size(self, queue: str) -> int:
        """Return the number of messages in a queue."""
        return len(await asyncio.to_thread(self._queued_files, queue))

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
        if not exchange:
            # Default exchange: routing_key is the queue name
            await self._put_message(routing_key, message)
            return

        for binding in await self._load_exchange_bindings(exchange):
            if binding.routing_key == routing_key:
                await self._put_message(binding.queue, message)

    async def _fanout_publish(
        self,
        exchange: str,
        message: bytes,
    ) -> None:
        """Publish to fanout exchange."""
        for binding in await self._load_exchange_bindings(exchange):
            await self._put_message(binding.queue, message)

    async def _topic_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        """Publish to topic exchange with pattern matching."""
        for binding in await self._load_exchange_bindings(exchange):
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
        # Zero padded so that the names of two messages sort in publish order.
        filepath = self._data_folder_out / f"{monotonic_ns():020d}_{uuid.uuid4()}.{queue}{MESSAGE_SUFFIX}"

        try:
            await asyncio.to_thread(filepath.write_bytes, message)
        except OSError as e:
            raise ChannelError(f"Cannot write message to {filepath}: {e}") from e

    # Moving a file into the inflight directory claims it and keeps it restorable until ack.

    @staticmethod
    def _move(filepath: Path, folder: Path) -> Path | None:
        """Move a file into `folder`, returning None if it is not there."""
        try:
            return filepath.replace(folder / filepath.name)
        except FileNotFoundError:
            return None

    def _claim(self, filepath: Path) -> Path | None:
        """Take a queued message, or None if another consumer got there first."""
        return self._move(filepath, self._inflight_folder)

    def _retire(self, filepath: Path) -> None:
        """Dispose of a message that has been dealt with."""
        if self._store_processed:
            self._move(filepath, self._processed_folder)
        else:
            filepath.unlink(missing_ok=True)

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        """Get a single message from a queue."""
        for filepath in await asyncio.to_thread(self._queued_files, queue):
            claimed = await asyncio.to_thread(self._claim, filepath)
            if claimed is None:
                continue

            try:
                data = await asyncio.to_thread(claimed.read_bytes)
            except OSError as e:
                raise ChannelError(f"Cannot read message from {claimed}: {e}") from e

            message = self._create_message(queue, data, no_ack, accept, claimed)
            if no_ack:
                # Nothing will ever acknowledge it.
                await asyncio.to_thread(self._retire, claimed)
            return message

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
        """Deliver one queued message to its consumer.

        ``timeout=0`` polls the directory once and returns. A positive timeout
        keeps polling for at most that long, and None polls indefinitely.
        Returns True if a message was delivered.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            if await self._deliver_ready():
                return True

            if deadline is None:
                wait = POLL_INTERVAL
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                wait = min(remaining, POLL_INTERVAL)
            await asyncio.sleep(wait)

    async def _deliver_ready(self) -> bool:
        """Deliver one queued message, taking the consumers in turn."""
        # Snapshot: a consumer can be cancelled while a message file is read.
        consumers = list(self._consumers.items())
        if not consumers:
            return False

        start = self._next_consumer % len(consumers)
        for offset in range(len(consumers)):
            index = (start + offset) % len(consumers)
            tag, (queue, callback, no_ack) = consumers[index]
            if tag not in self._consumers:
                continue
            message = await self.get(queue, no_ack=no_ack)
            if message is None:
                continue
            self._next_consumer = index + 1
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
        filepath: Path | None = None,
    ) -> Message:
        """Create a Message object from raw data."""
        envelope = decode_envelope(data, queue)
        delivery_tag = self._next_delivery_tag()

        if not no_ack and filepath is not None:
            self._unacked[delivery_tag] = (queue, filepath)

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
        for tag in self._tags_up_to(delivery_tag) if multiple else [delivery_tag]:
            entry = self._unacked.pop(tag, None)
            if entry is not None:
                await asyncio.to_thread(self._retire, entry[1])

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
        if entry is None:
            return
        _, filepath = entry
        if requeue:
            await asyncio.to_thread(self._move, filepath, self._data_folder_in)
        else:
            await asyncio.to_thread(self._retire, filepath)

    async def basic_recover(self, requeue: bool = True) -> None:
        """Recover unacknowledged messages."""
        outstanding = list(self._unacked.values())
        self._unacked.clear()
        for _queue, filepath in outstanding:
            if requeue:
                await asyncio.to_thread(self._move, filepath, self._data_folder_in)
            else:
                await asyncio.to_thread(self._retire, filepath)


_Channel = Channel


class Transport(BaseTransport):
    """Pure asyncio filesystem transport.

    Uses the filesystem for message storage.
    """

    Channel = _Channel
    default_port = None

    driver_type = "filesystem"
    driver_name = "filesystem"

    def __init__(self, url: str = "filesystem://", **options: Any):
        super().__init__(url, **options)
        self._channels: list[Channel] = []
        self._connected = False

        # Extract transport options
        self._data_folder_in = options.get("data_folder_in", "data_in")
        self._data_folder_out = options.get("data_folder_out", "data_out")
        self._store_processed = options.get("store_processed", False)
        self._processed_folder = options.get("processed_folder", "processed")
        self._control_folder = options.get("control_folder", "control")

    async def connect(self) -> None:
        """Connect (ensures directories exist)."""
        # The channel is the one that knows which directories it works in.
        await self._new_channel()._ensure_directories()
        self._connected = True
        logger.debug("Filesystem transport connected")

    async def close(self) -> None:
        """Close the transport and all channels."""
        for channel in self._channels:
            await channel.close()
        self._channels.clear()
        self._connected = False

    def _new_channel(self) -> _Channel:
        return Channel(
            data_folder_in=self._data_folder_in,
            data_folder_out=self._data_folder_out,
            store_processed=self._store_processed,
            processed_folder=self._processed_folder,
            control_folder=self._control_folder,
        )

    async def create_channel(self) -> _Channel:
        """Create a new channel."""
        if not self._connected:
            await self.connect()

        channel = self._new_channel()
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
        """Forget every declared exchange.

        Test hook: exchange declarations are process-wide and are not written
        to the control folder, so a suite that wants each test to start clean
        calls this between tests.
        """
        Channel._exchanges.clear()
