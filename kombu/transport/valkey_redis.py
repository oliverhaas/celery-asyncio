"""Pure asyncio Valkey/Redis transport with priority queues, Streams fanout, and delayed delivery.

This transport uses valkey.asyncio or redis.asyncio for all operations and provides:
1. BZMPOP + sorted sets for regular queues — full 0-255 priority support
2. Streams for fanout exchanges — reliable, not lossy like PUB/SUB
3. Native delayed delivery — delay integrated into sorted set scoring
4. Per-message hash storage — reliability and visibility tracking
5. Global key prefixing — multi-tenant support
6. FAST/SLOW consume mode — atomic Lua script with BZMPOP fallback
7. Atomic ack — Lua script for ZREM+DEL atomicity
8. Restore count tracking — max_restore_count enforcement

Supports both valkey-py and redis-py client libraries. The URL scheme selects the
preferred library (with automatic fallback if only one is installed):

- ``valkey://`` / ``valkeys://`` → prefer valkey-py, fallback redis-py
- ``redis://`` / ``rediss://`` → prefer redis-py, fallback valkey-py

Requires Valkey 7.2+ or Redis 7.0+ for BZMPOP support.

Connection String
=================
.. code-block::

    valkey://[USER:PASSWORD@]ADDRESS[:PORT][/DB]
    valkeys://[USER:PASSWORD@]ADDRESS[:PORT][/DB]
    redis://[USER:PASSWORD@]ADDRESS[:PORT][/DB]
    rediss://[USER:PASSWORD@]ADDRESS[:PORT][/DB]

Transport Options
=================
* ``global_keyprefix``: Global prefix for all keys (multi-tenant)
* ``visibility_timeout``: Seconds before unacked messages are restored (default: 300)
* ``message_ttl``: TTL for per-message hashes in seconds (-1 = no TTL)
* ``stream_maxlen``: Maximum stream length for fanout streams (default: 10000)
* ``fanout_prefix``: Prefix for fanout stream keys (default: '/{db}.')
* ``max_restore_count``: Max times a message can be restored before being dropped (None = no limit)
* ``block_timeout``: Server-side BZMPOP/XREAD BLOCK duration per consumer iteration in seconds (default: 10)
* ``credential_provider``: A CredentialProvider instance or dotted import path
* ``socket_timeout``: Socket timeout in seconds
* ``socket_connect_timeout``: Socket connection timeout in seconds
* ``health_check_interval``: Health check interval for connections
* ``max_connections``: Maximum connections in pool
"""

import asyncio
import base64
import re
import urllib.parse
import uuid
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any

from kombu.log import get_logger
from kombu.message import Message
from kombu.transport._valkey_redis_compat import (
    get_all_channel_errors,
    get_all_connection_errors,
    normalize_url,
    resolve_async_lib,
    resolve_exceptions,
    resolve_lib,
)
from kombu.transport.base import Transport as BaseTransport
from kombu.utils.json import dumps as json_dumps
from kombu.utils.json import loads as json_loads

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

    from kombu.entity import Exchange, Queue

__all__ = ("Channel", "Transport")

logger = get_logger("kombu.transport.valkey_redis")

# ---------------------------------------------------------------------------
# Constants (ported from celery-redis-plus constants.py)
# ---------------------------------------------------------------------------

QUEUE_KEY_PREFIX = "queue:"
MESSAGE_KEY_PREFIX = "message:"
MESSAGES_INDEX_PREFIX = "messages_index:"
BINDING_KEY_PREFIX = "_kombu.binding."

PRIORITY_SCORE_MULTIPLIER = 10**13
MIN_PRIORITY = 0
MAX_PRIORITY = 255
DEFAULT_PRIORITY = 0

DEFAULT_VISIBILITY_TIMEOUT = 300
DEFAULT_HEALTH_CHECK_INTERVAL = 25
DEFAULT_STREAM_MAXLEN = 10000
DEFAULT_REQUEUE_CHECK_INTERVAL = 60
DEFAULT_REQUEUE_BATCH_LIMIT = 1000
DEFAULT_MESSAGE_TTL = -1
MIN_QUEUE_EXPIRES = 10_000
DEFAULT_MAX_RESTORE_COUNT: int | None = None

# Default server-side block duration for BZMPOP/XREAD inside a single consumer
# iteration. Overridable via the `block_timeout` transport option.
DEFAULT_BLOCK_TIMEOUT = 10.0

# Additional headroom layered on top of the configured `block_timeout` for the
# asyncio.wait safety net. We trust Redis to honour BLOCK and return within
# block_timeout; the safety net only fires if the client connection or server
# has hung beyond block_timeout + this headroom.
CONSUMER_WAIT_HEADROOM = 30.0

# Additional headroom layered on top of `block_timeout` for close()'s graceful
# drain. The iteration should complete within block_timeout; we give it a small
# extra window before cancelling as a last resort. Cancellation during close
# may strand a message, but visibility-timeout restore recovers them on the
# next worker startup.
CLOSE_DRAIN_HEADROOM = 2.0

DEFAULT_EXCHANGE = ""
DEFAULT_FANOUT_PREFIX = "/{db}."

# Separator for binding encoding (cross-compatible with celery-redis-plus)
BINDING_SEP = "\x06\x16"

# ---------------------------------------------------------------------------
# Lua scripts (loaded from files copied from celery-redis-plus)
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).parent
_ENQUEUE_DUE_MESSAGES_LUA = (_PACKAGE_DIR / "transport_enqueue_due_messages.lua").read_text()
_REQUEUE_MESSAGE_LUA = (_PACKAGE_DIR / "transport_requeue_message.lua").read_text()
_CONSUME_MESSAGE_LUA = (_PACKAGE_DIR / "transport_consume_message.lua").read_text()
_ACK_MESSAGE_LUA = (_PACKAGE_DIR / "transport_ack_message.lua").read_text()

# ---------------------------------------------------------------------------
# Valkey/Redis error tuples (from ALL installed libraries for catch-all)
# ---------------------------------------------------------------------------

_redis_connection_errors = get_all_connection_errors()
_redis_channel_errors = get_all_channel_errors()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue_score(priority: int, timestamp: float | None = None) -> float:
    """Compute sorted set score for queue ordering.

    Higher priority number → lower score → popped first (RabbitMQ semantics).
    Within same priority, earlier timestamp → lower score → FIFO.
    """
    if timestamp is None:
        timestamp = time()
    priority = max(MIN_PRIORITY, min(MAX_PRIORITY, priority))
    return (MAX_PRIORITY - priority) * PRIORITY_SCORE_MULTIPLIER + int(timestamp * 1000)


def _topic_match(routing_key: str, pattern: str) -> bool:
    """Match routing key against AMQP topic pattern (* and #).

    Supports:
    - * matches exactly one word
    - # matches zero or more words (including zero)
    """
    regex = pattern.replace(".", r"\.")
    regex = regex.replace("*", r"[^.]+")
    regex = regex.replace(r"\.#", r"(\..*)?")  # dot-hash: zero or more words
    regex = regex.replace(r"#\.", r"(.*\.)?")  # hash-dot: zero or more words
    regex = regex.replace("#", r".*")  # standalone hash
    return bool(re.match(f"^{regex}$", routing_key))


def _parse_db_from_url(url: str) -> str:
    """Extract database number from Redis URL."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    return path or "0"


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class Channel:
    """Redis channel with BZMPOP priority queues, Streams fanout, and delayed delivery.

    Each channel manages its own consumers, message delivery, and
    background tasks for visibility heartbeat and delayed enqueue.

    Supports FAST/SLOW consume mode:
    - FAST: atomic Lua script (ZPOPMIN + ZADD index + HMGET) — non-blocking
    - SLOW: blocking BZMPOP fallback when queues are empty
    """

    _warned_expires_clamp = False

    def __init__(self, transport: Transport, connection_id: str) -> None:
        self._transport = transport
        self._connection_id = connection_id
        self._channel_id = str(uuid.uuid4())
        self._closed = False

        # Consumer state: tag → (queue, callback, no_ack)
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self.no_ack_consumers: set[str] | None = set()

        # Exchange / binding state
        self._exchanges: dict[str, dict] = {}
        self._bindings: dict[str, list[tuple[str, str]]] = {}

        # Fanout state
        self._fanout_queues: dict[str, tuple[str, str]] = {}  # queue → (exchange, rk)
        self._fanout_to_queue: dict[str, str] = {}  # exchange → queue
        self.active_fanout_queues: set[str] = set()
        self.auto_delete_queues: set[str] = set()
        self._stream_offsets: dict[str, str] = {}  # stream_key → last ID

        # Message tracking
        self._delivered: dict[str, tuple[str, Message]] = {}  # tag → (queue, msg)
        self._fanout_tags: set[str] = set()
        self._delivery_tag_counter = 0

        # Per-queue TTL state
        self._expires: dict[str, int] = {}  # queue → TTL ms
        self._message_ttls: dict[str, int] = {}  # queue → message TTL ms

        # Config from transport options
        opts = transport._options
        self._global_keyprefix: str = opts.get("global_keyprefix", "")
        self._visibility_timeout: float = opts.get(
            "visibility_timeout",
            DEFAULT_VISIBILITY_TIMEOUT,
        )
        self._message_ttl: int = opts.get("message_ttl", DEFAULT_MESSAGE_TTL)
        self._stream_maxlen: int = opts.get("stream_maxlen", DEFAULT_STREAM_MAXLEN)
        self._max_restore_count: int | None = opts.get(
            "max_restore_count",
            DEFAULT_MAX_RESTORE_COUNT,
        )

        # Fanout prefix: True → default, False → none, str → custom
        fanout_prefix = opts.get("fanout_prefix", True)
        if fanout_prefix is True:
            self._fanout_prefix = DEFAULT_FANOUT_PREFIX.format(db=transport._db)
        elif fanout_prefix:
            self._fanout_prefix = str(fanout_prefix).format(db=transport._db)
        else:
            self._fanout_prefix = ""

        # FAST/SLOW consume mode
        self._consume_fast_mode: bool = True

        # Server-side block duration for BZMPOP/XREAD inside a single consumer
        # iteration.
        self._block_timeout: float = opts.get("block_timeout", DEFAULT_BLOCK_TIMEOUT)

        # Long-lived consumer iteration tasks; created lazily by drain_events.
        # Each task runs a single FAST→SLOW (or XREAD) cycle bounded by
        # `_block_timeout`, then exits. drain_events restarts them. They are
        # never cancelled in the hot path; close() sets _closing and awaits
        # them to drain naturally.
        # `_started_at` and `_stall_warned` provide per-task stall detection:
        # when one iteration hangs while the other keeps delivering, the
        # hung-task warning still fires (asyncio.wait's FIRST_COMPLETED
        # would otherwise mask it).
        self._consume_iter_task: asyncio.Task | None = None
        self._consume_iter_started_at: float = 0.0
        self._consume_iter_stall_warned: bool = False
        self._xread_iter_task: asyncio.Task | None = None
        self._xread_iter_started_at: float = 0.0
        self._xread_iter_stall_warned: bool = False
        self._closing: bool = False

        # Periodic task handles
        self._enqueue_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._expires_task: asyncio.Task | None = None

        # Lua script handles (registered lazily)
        self._enqueue_script = None
        self._requeue_script = None
        self._consume_script = None
        self._ack_script = None

    # ---- key helpers -------------------------------------------------------

    def _prefixed(self, key: str) -> str:
        """Add global key prefix."""
        if self._global_keyprefix:
            return f"{self._global_keyprefix}{key}"
        return key

    def _unprefixed(self, key: str) -> str:
        """Strip global key prefix."""
        if self._global_keyprefix and key.startswith(self._global_keyprefix):
            return key[len(self._global_keyprefix) :]
        return key

    def _queue_key(self, queue: str) -> str:
        return self._prefixed(f"{QUEUE_KEY_PREFIX}{queue}")

    def _message_key(self, delivery_tag: str) -> str:
        return self._prefixed(f"{MESSAGE_KEY_PREFIX}{delivery_tag}")

    def _messages_index_key(self, queue: str) -> str:
        return self._prefixed(f"{MESSAGES_INDEX_PREFIX}{queue}")

    def _binding_key(self, exchange: str) -> str:
        return self._prefixed(f"{BINDING_KEY_PREFIX}{exchange}")

    def _fanout_stream_key(self, exchange: str) -> str:
        return self._prefixed(f"{self._fanout_prefix}{exchange}")

    # ---- client access -----------------------------------------------------

    @property
    def client(self):
        """Main Redis client (BZMPOP, sorted set ops, publish)."""
        return self._transport._client

    @property
    def subclient(self):
        """Dedicated client for XREAD BLOCK (fanout streams)."""
        return self._transport._subclient

    def _next_delivery_tag(self) -> str:
        self._delivery_tag_counter += 1
        return f"{self._channel_id}.{self._delivery_tag_counter}"

    # ---- Lua scripts -------------------------------------------------------

    async def _get_enqueue_script(self):
        if self._enqueue_script is None:
            self._enqueue_script = self.client.register_script(
                _ENQUEUE_DUE_MESSAGES_LUA,
            )
        return self._enqueue_script

    async def _get_requeue_script(self):
        if self._requeue_script is None:
            self._requeue_script = self.client.register_script(
                _REQUEUE_MESSAGE_LUA,
            )
        return self._requeue_script

    async def _get_consume_script(self):
        if self._consume_script is None:
            self._consume_script = self.client.register_script(
                _CONSUME_MESSAGE_LUA,
            )
        return self._consume_script

    async def _get_ack_script(self):
        if self._ack_script is None:
            self._ack_script = self.client.register_script(
                _ACK_MESSAGE_LUA,
            )
        return self._ack_script

    # ---- exchange operations -----------------------------------------------

    async def declare_exchange(self, exchange: Exchange) -> None:
        self._exchanges[exchange.name] = {
            "type": exchange.type,
            "durable": exchange.durable,
            "auto_delete": exchange.auto_delete,
            "arguments": exchange.arguments,
        }

    async def exchange_delete(self, exchange: str) -> None:
        self._exchanges.pop(exchange, None)
        self._bindings.pop(exchange, None)
        await self.client.delete(self._binding_key(exchange))

    # ---- queue operations --------------------------------------------------

    async def declare_queue(self, queue: Queue) -> str:
        name = queue.name or f"amq.gen-{uuid.uuid4()}"
        queue.name = name

        # Parse queue arguments for TTL
        arguments = getattr(queue, "queue_arguments", None) or {}
        if not arguments:
            arguments = getattr(queue, "arguments", None) or {}

        x_expires = arguments.get("x-expires")
        if x_expires is not None and name not in self._expires:
            x_expires = int(x_expires)
            if x_expires < MIN_QUEUE_EXPIRES:
                if not self._warned_expires_clamp:
                    logger.warning(
                        "x-expires %dms is below minimum %dms (10s), clamping."
                        " This warning is shown once; other queues may also"
                        " be affected.",
                        x_expires,
                        MIN_QUEUE_EXPIRES,
                    )
                    Channel._warned_expires_clamp = True
                x_expires = MIN_QUEUE_EXPIRES
            self._expires[name] = x_expires
            self._update_expires_task()

        x_message_ttl = arguments.get("x-message-ttl")
        if x_message_ttl is not None and name not in self._message_ttls:
            self._message_ttls[name] = int(x_message_ttl)

        if getattr(queue, "auto_delete", False):
            self.auto_delete_queues.add(name)

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
        if exchange not in self._bindings:
            self._bindings[exchange] = []
        binding = (queue, routing_key or queue)
        if binding not in self._bindings[exchange]:
            self._bindings[exchange].append(binding)

        # Detect fanout
        exchange_meta = self._exchanges.get(exchange, {})
        if exchange_meta.get("type") == "fanout":
            self._fanout_queues[queue] = (exchange, routing_key.replace("#", "*"))

        # Store in Redis with sep-delimited format
        binding_data = BINDING_SEP.join([routing_key or "", routing_key or "", queue])
        await self.client.sadd(self._binding_key(exchange), binding_data)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        if exchange in self._bindings:
            binding = (queue, routing_key or queue)
            if binding in self._bindings[exchange]:
                self._bindings[exchange].remove(binding)

        binding_data = BINDING_SEP.join([routing_key or "", routing_key or "", queue])
        await self.client.srem(self._binding_key(exchange), binding_data)

    async def queue_purge(self, queue: str) -> int:
        """Purge all messages from a queue, cleaning up message hashes."""
        queue_key = self._queue_key(queue)
        index_key = self._messages_index_key(queue)
        size = await self.client.zcard(queue_key)

        # Collect delivery tags from both queue and index to clean up message hashes.
        # Index may have tags not in queue (native delayed messages waiting for delivery).
        tags: set[str] = set()
        raw_queue_tags = await self.client.zrange(queue_key, 0, -1)
        for t in raw_queue_tags:
            tags.add(t.decode() if isinstance(t, bytes) else t)
        raw_index_tags = await self.client.zrange(index_key, 0, -1)
        for t in raw_index_tags:
            tags.add(t.decode() if isinstance(t, bytes) else t)

        async with self.client.pipeline(transaction=False) as pipe:
            await pipe.delete(queue_key)
            await pipe.delete(index_key)
            for tag in tags:
                await pipe.delete(self._message_key(tag))
            await pipe.execute()

        return size

    async def queue_delete(
        self,
        queue: str,
        if_unused: bool = False,
        if_empty: bool = False,
    ) -> int:
        queue_key = self._queue_key(queue)

        if if_empty:
            size = await self.client.zcard(queue_key)
            if size > 0:
                return 0

        size = await self.client.zcard(queue_key)
        index_key = self._messages_index_key(queue)

        # Collect delivery tags from both queue and index to clean up message hashes
        tags: set[str] = set()
        raw_queue_tags = await self.client.zrange(queue_key, 0, -1)
        for t in raw_queue_tags:
            tags.add(t.decode() if isinstance(t, bytes) else t)
        raw_index_tags = await self.client.zrange(index_key, 0, -1)
        for t in raw_index_tags:
            tags.add(t.decode() if isinstance(t, bytes) else t)

        async with self.client.pipeline(transaction=False) as pipe:
            await pipe.delete(queue_key)
            await pipe.delete(index_key)
            for tag in tags:
                await pipe.delete(self._message_key(tag))
            await pipe.execute()

        self._expires.pop(queue, None)
        self._message_ttls.pop(queue, None)
        self.auto_delete_queues.discard(queue)

        for exch, bindings in list(self._bindings.items()):
            self._bindings[exch] = [(q, rk) for q, rk in bindings if q != queue]

        return size

    # ---- publish -----------------------------------------------------------

    async def publish(
        self,
        message: bytes,
        exchange: str,
        routing_key: str,
        **kwargs: Any,
    ) -> None:
        exchange = exchange or DEFAULT_EXCHANGE
        exchange_meta = self._exchanges.get(exchange, {"type": "direct"})
        exchange_type = exchange_meta.get("type", "direct")

        if exchange_type == "fanout":
            await self._fanout_publish(exchange, message)
        elif exchange_type == "topic":
            await self._topic_publish(exchange, routing_key, message)
        else:
            await self._direct_publish(exchange, routing_key, message)

    async def _load_bindings(self, exchange: str) -> list[tuple[str, str]]:
        """Load bindings from Redis, supporting both sep and JSON formats."""
        members = await self.client.smembers(self._binding_key(exchange))
        bindings = []
        for member in members:
            if isinstance(member, bytes):
                member = member.decode()
            if BINDING_SEP in member:
                parts = member.split(BINDING_SEP)
                while len(parts) < 3:
                    parts.append("")
                bindings.append((parts[2], parts[0]))  # (queue, routing_key)
            else:
                try:
                    data = json_loads(member)
                    bindings.append((data["queue"], data.get("routing_key", "")))
                except (ValueError, KeyError):  # fmt: skip
                    pass
        return bindings

    async def _direct_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        if exchange:
            bindings = await self._load_bindings(exchange)
            for queue, rk in bindings:
                if rk == routing_key:
                    await self._put_message(queue, message)
        else:
            # Default exchange: routing_key is the queue name
            await self._put_message(routing_key, message)

    async def _fanout_publish(self, exchange: str, message: bytes) -> None:
        stream_key = self._fanout_stream_key(exchange)
        payload = message.decode("utf-8") if isinstance(message, bytes) else message
        await self.client.xadd(
            name=stream_key,
            fields={"uuid": str(uuid.uuid4()), "payload": payload},
            id="*",
            maxlen=self._stream_maxlen,
            approximate=True,
        )

    async def _topic_publish(
        self,
        exchange: str,
        routing_key: str,
        message: bytes,
    ) -> None:
        bindings = await self._load_bindings(exchange)
        for queue, pattern in bindings:
            if _topic_match(routing_key, pattern):
                await self._put_message(queue, message)

    async def _put_message(self, queue: str, raw_message: bytes) -> None:
        """Publish a message to a queue via sorted set with per-message hash."""
        # Parse envelope
        try:
            payload = json_loads(raw_message)
        except (ValueError, TypeError):  # fmt: skip
            payload = {
                "body": raw_message.decode("utf-8", errors="replace")
                if isinstance(raw_message, bytes)
                else str(raw_message),
                "properties": {},
                "headers": {},
            }

        props = payload.setdefault("properties", {})
        priority = int(props.get("priority", DEFAULT_PRIORITY))
        delivery_tag = props.get("delivery_tag") or str(uuid.uuid4())
        props["delivery_tag"] = delivery_tag

        now = time()

        # Native delayed delivery (only for delays > requeue interval)
        eta_timestamp: float | None = props.get("eta")
        is_native_delayed = eta_timestamp is not None and (float(eta_timestamp) - now) > DEFAULT_REQUEUE_CHECK_INTERVAL
        if is_native_delayed:
            eta_timestamp = float(eta_timestamp)
        visible_at = eta_timestamp if is_native_delayed else now

        queue_score = _queue_score(priority, visible_at)
        # queue_at = time when enqueue_due_messages will pick up this message.
        # Adding RCI ensures the message won't be restored prematurely before
        # the next enqueue cycle runs.
        queue_at = (
            eta_timestamp if is_native_delayed else now + self._visibility_timeout + DEFAULT_REQUEUE_CHECK_INTERVAL
        )

        message_key = self._message_key(delivery_tag)
        index_key = self._messages_index_key(queue)
        queue_key = self._queue_key(queue)

        async with self.client.pipeline(transaction=False) as pipe:
            # Per-message hash
            await pipe.hset(
                message_key,
                mapping={
                    "payload": json_dumps(payload),
                    "routing_key": queue,
                    "priority": priority,
                    "redelivered": 0,
                    "native_delayed": 1 if is_native_delayed else 0,
                    "eta": eta_timestamp or 0,
                    "restore_count": 0,
                },
            )

            # Message TTL
            effective_ttl = self._message_ttl
            if queue in self._message_ttls:
                queue_ttl_s = self._message_ttls[queue] // 1000
                effective_ttl = queue_ttl_s if effective_ttl < 0 else min(effective_ttl, queue_ttl_s)
            if effective_ttl >= 0:
                await pipe.expire(message_key, effective_ttl)

            # Messages index (visibility tracking)
            await pipe.zadd(index_key, {delivery_tag: queue_at})

            # Queue sorted set (skip if native delayed)
            if not is_native_delayed:
                await pipe.zadd(queue_key, {delivery_tag: queue_score})

            # Queue TTL
            if queue in self._expires:
                ttl_ms = self._expires[queue]
                await pipe.pexpire(queue_key, ttl_ms)
                await pipe.pexpire(index_key, ttl_ms)

            await pipe.execute()

    # ---- consumer operations -----------------------------------------------

    async def basic_consume(
        self,
        queue: str,
        callback: Callable[[Message], Any],
        consumer_tag: str | None = None,
        no_ack: bool = False,
    ) -> str:
        if consumer_tag is None:
            consumer_tag = str(uuid.uuid4())

        self._consumers[consumer_tag] = (queue, callback, no_ack)

        if no_ack and self.no_ack_consumers is not None:
            self.no_ack_consumers.add(consumer_tag)

        if queue in self._fanout_queues:
            self.active_fanout_queues.add(queue)
            exchange, _ = self._fanout_queues[queue]
            self._fanout_to_queue[exchange] = queue

        self._start_periodic_tasks()
        return consumer_tag

    async def basic_cancel(self, consumer_tag: str) -> None:
        entry = self._consumers.pop(consumer_tag, None)
        if entry:
            queue, _, _ = entry
            self.active_fanout_queues.discard(queue)
            if queue in self._fanout_queues:
                exchange, _ = self._fanout_queues[queue]
                self._fanout_to_queue.pop(exchange, None)
        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)

    # ---- drain_events (FAST/SLOW consume) ----------------------------------

    async def drain_events(self, timeout: float | None = None) -> bool:
        """Wait for a single message to be delivered to one of this channel's
        consumers.

        Consumer iterations are long-lived and bounded by Redis-side
        `block_timeout` — never cancelled mid-flight. They complete on their
        own; the asyncio.wait safety net at `block_timeout + CONSUMER_WAIT_HEADROOM`
        only fires if the client connection or server has hung. The caller's
        `timeout` parameter only affects the timeout=0 non-blocking fast path.
        """
        if self._closing or not self._consumers:
            await asyncio.sleep(0.1)
            return False

        effective_timeout = timeout if timeout is not None else 1.0

        # Non-blocking fast path: only try FAST consume.
        if effective_timeout == 0:
            regular_queues = self._regular_queues()
            if regular_queues:
                return await self._fast_consume(regular_queues)
            return False

        self._ensure_consumer_tasks()
        tasks = [t for t in (self._consume_iter_task, self._xread_iter_task) if t is not None]
        if not tasks:
            await asyncio.sleep(0.1)
            return False

        safety_timeout = self._block_timeout + CONSUMER_WAIT_HEADROOM
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=safety_timeout,
        )

        # Per-task stall warning. asyncio.wait with FIRST_COMPLETED returns
        # as soon as ANY task completes, so a single hung iteration alongside
        # a healthy one would otherwise be silent. We deliberately don't
        # cancel: a cancel mid-FAST-script or post-BZMPOP would strand the
        # message that's about to be delivered. The iteration is left pending;
        # if Redis recovers it completes naturally and the next drain_events
        # call picks it up.
        self._warn_stalled_iterations()

        if not done:
            return False

        delivered = False
        for task in done:
            try:
                if task.result():
                    delivered = True
            except Exception as exc:
                logger.debug("consumer iteration error: %s", exc)
            if task is self._consume_iter_task:
                self._consume_iter_task = None
            elif task is self._xread_iter_task:
                self._xread_iter_task = None
        return delivered

    def _regular_queues(self) -> list[str]:
        return list(
            dict.fromkeys(q for q, _cb, _no_ack in self._consumers.values() if q not in self.active_fanout_queues),
        )

    def _ensure_consumer_tasks(self) -> None:
        if self._closing:
            return
        loop = asyncio.get_running_loop()
        regular_queues = self._regular_queues()
        if regular_queues and (self._consume_iter_task is None or self._consume_iter_task.done()):
            self._consume_iter_task = asyncio.create_task(
                self._safe_consume_iter(regular_queues),
            )
            self._consume_iter_started_at = loop.time()
            self._consume_iter_stall_warned = False
        if self.active_fanout_queues and (self._xread_iter_task is None or self._xread_iter_task.done()):
            self._xread_iter_task = asyncio.create_task(
                self._safe_xread_iter(),
            )
            self._xread_iter_started_at = loop.time()
            self._xread_iter_stall_warned = False

    def _warn_stalled_iterations(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        threshold = self._block_timeout + CONSUMER_WAIT_HEADROOM
        for kind, task_attr, started_attr, warned_attr in (
            ("consume_regular", "_consume_iter_task", "_consume_iter_started_at", "_consume_iter_stall_warned"),
            ("xread_wait", "_xread_iter_task", "_xread_iter_started_at", "_xread_iter_stall_warned"),
        ):
            task = getattr(self, task_attr)
            if (
                task is not None
                and not task.done()
                and not getattr(self, warned_attr)
                and now - getattr(self, started_attr) > threshold
            ):
                logger.warning(
                    "%s iteration has been pending for %.1fs (> %.1fs); Redis "
                    "BLOCK reply may be stalled. Iteration left running.",
                    kind,
                    now - getattr(self, started_attr),
                    threshold,
                )
                setattr(self, warned_attr, True)

    async def _safe_consume_iter(self, queues: list[str]) -> bool:
        try:
            return await self._consume_regular(queues, self._block_timeout)
        except Exception as exc:
            logger.debug("consume iteration error: %s", exc)
            # Back off so drain_events doesn't tight-loop on a persistent fault.
            await asyncio.sleep(min(self._block_timeout, 0.1))
            return False

    async def _safe_xread_iter(self) -> bool:
        try:
            return await self._xread_wait(self._block_timeout)
        except Exception as exc:
            logger.debug("xread iteration error: %s", exc)
            await asyncio.sleep(min(self._block_timeout, 0.1))
            return False

    async def _consume_regular(self, queues: list[str], timeout: float) -> bool:
        """Consume from regular queues using FAST/SLOW mode.

        FAST: atomic Lua script (ZPOPMIN + ZADD index + HMGET) — non-blocking
        SLOW: blocking BZMPOP fallback when FAST returns nil (all queues empty)

        When timeout=0 (non-blocking poll), only FAST mode is attempted
        to avoid BZMPOP blocking.
        """
        if self._consume_fast_mode or timeout == 0:
            result = await self._fast_consume(queues)
            if result:
                return True
            if timeout == 0:
                # Non-blocking poll: don't fall through to blocking BZMPOP
                return False
            # FAST returned nil — all queues empty, switch to SLOW
            self._consume_fast_mode = False

        # SLOW mode: blocking BZMPOP
        delivered = await self._slow_consume(queues, timeout)
        if delivered:
            self._consume_fast_mode = True  # Switch back to FAST
        return delivered

    async def _fast_consume(self, queues: list[str]) -> bool:
        """FAST mode: atomic Lua script for non-blocking consume."""
        queue_keys = [self._queue_key(q) for q in queues]
        new_queue_at = time() + self._visibility_timeout + DEFAULT_REQUEUE_CHECK_INTERVAL

        try:
            script = await self._get_consume_script()
            result = await script(
                keys=queue_keys,
                args=[
                    self._global_keyprefix,
                    MESSAGE_KEY_PREFIX,
                    str(new_queue_at),
                    MESSAGES_INDEX_PREFIX,
                    *queues,
                ],
            )
        except Exception as exc:
            logger.debug("FAST consume error: %s", exc)
            return False

        if not result:
            return False

        queue_name, delivery_tag, payload_json, restore_count = self._parse_consume_result(result)
        payload = json_loads(payload_json)

        # Inject x-restore-count header if message was restored
        if restore_count > 0:
            headers = payload.setdefault("headers", {})
            headers["x-restore-count"] = restore_count

        message = self._create_message(queue_name, payload, delivery_tag)
        await self._deliver_to_consumer(queue_name, message)
        return True

    async def _slow_consume(self, queues: list[str], timeout: float) -> bool:
        """SLOW mode: blocking BZMPOP with pipeline index refresh + HMGET."""
        queue_keys = [self._queue_key(q) for q in queues]

        try:
            result = await self.client.bzmpop(
                timeout,
                len(queue_keys),
                queue_keys,
                min=True,
            )
        except Exception as exc:
            logger.debug("BZMPOP error: %s", exc)
            # Defensive: prevent drain_events from tight-looping on connection
            # errors. Successful BZMPOP timeouts already wait `timeout` seconds.
            await asyncio.sleep(min(timeout, 0.1))
            return False

        if not result:
            return False

        queue_key_raw, members = result
        queue_key = queue_key_raw.decode() if isinstance(queue_key_raw, bytes) else queue_key_raw
        queue_key = self._unprefixed(queue_key)
        queue = queue_key.removeprefix(QUEUE_KEY_PREFIX)

        delivery_tag_raw, _score = members[0]
        delivery_tag = delivery_tag_raw.decode() if isinstance(delivery_tag_raw, bytes) else delivery_tag_raw

        # Pipeline: refresh index + fetch payload and restore_count
        message_key = self._message_key(delivery_tag)
        index_key = self._messages_index_key(queue)
        new_queue_at = time() + self._visibility_timeout + DEFAULT_REQUEUE_CHECK_INTERVAL

        async with self.client.pipeline(transaction=False) as pipe:
            await pipe.zadd(index_key, {delivery_tag: new_queue_at}, xx=True)
            await pipe.hmget(message_key, "payload", "restore_count")
            results = await pipe.execute()

        payload_json = results[1][0]
        if not payload_json:
            # Hash expired — clean up index, try drain
            await self.client.zrem(index_key, delivery_tag)
            return await self._drain_expired_and_deliver(queue)

        payload = json_loads(payload_json)
        restore_count = int(results[1][1] or 0)
        if restore_count > 0:
            headers = payload.setdefault("headers", {})
            headers["x-restore-count"] = restore_count

        message = self._create_message(queue, payload, delivery_tag)
        await self._deliver_to_consumer(queue, message)
        return True

    def _parse_consume_result(self, result: list) -> tuple[str, str, str, int]:
        """Parse the result from consume_message Lua script.

        Returns (queue_name, delivery_tag, payload_json, restore_count).
        """
        queue_name = result[0].decode() if isinstance(result[0], bytes) else result[0]
        delivery_tag = result[1].decode() if isinstance(result[1], bytes) else result[1]
        payload_json = result[2].decode() if isinstance(result[2], bytes) else result[2]
        restore_count = int(result[3] or 0)
        return queue_name, delivery_tag, payload_json, restore_count

    async def _drain_expired_and_deliver(self, queue: str) -> bool:
        """Pop messages until a valid one is found or queue is empty."""
        queue_key = self._queue_key(queue)
        while True:
            result = await self.client.zpopmin(queue_key, count=1)
            if not result:
                return False
            delivery_tag_raw, _score = result[0]
            delivery_tag = delivery_tag_raw.decode() if isinstance(delivery_tag_raw, bytes) else delivery_tag_raw
            message_key = self._message_key(delivery_tag)

            fields = await self.client.hmget(message_key, "payload", "restore_count")
            if fields[0]:
                payload = json_loads(fields[0])
                restore_count = int(fields[1] or 0)
                if restore_count > 0:
                    headers = payload.setdefault("headers", {})
                    headers["x-restore-count"] = restore_count
                message = self._create_message(queue, payload, delivery_tag)
                await self._deliver_to_consumer(queue, message)
                return True
            # Expired — clean up index entry
            await self.client.zrem(self._messages_index_key(queue), delivery_tag)

    async def _xread_wait(self, timeout: float) -> bool:
        """Wait for fanout messages from Redis Streams."""
        streams: dict[str, str] = {}
        for queue in self.active_fanout_queues:
            if queue in self._fanout_queues:
                exchange, _ = self._fanout_queues[queue]
                stream_key = self._fanout_stream_key(exchange)
                streams[stream_key] = self._stream_offsets.get(stream_key, "$")

        if not streams:
            return False

        try:
            timeout_ms = int(timeout * 1000)
            result = await self.subclient.xread(
                streams,
                count=1,
                block=timeout_ms,
            )
        except Exception as exc:
            logger.debug("XREAD error: %s", exc)
            # Defensive: prevent drain_events from tight-looping on connection
            # errors. Successful XREAD timeouts already wait `block` ms.
            await asyncio.sleep(min(timeout, 0.1))
            return False

        if not result:
            return False

        for stream_bytes, messages in result:
            stream_key = stream_bytes.decode() if isinstance(stream_bytes, bytes) else stream_bytes
            for message_id, fields in messages:
                msg_id = message_id.decode() if isinstance(message_id, bytes) else message_id

                # Update stream offsets
                self._stream_offsets[stream_key] = msg_id
                unprefixed = self._unprefixed(stream_key)
                if unprefixed != stream_key:
                    self._stream_offsets[unprefixed] = msg_id

                # Find which queue this stream belongs to
                queue_name = None
                for q, (exch, _) in self._fanout_queues.items():
                    fs = self._fanout_stream_key(exch)
                    unprefixed_stream = self._unprefixed(stream_key)
                    if fs in (stream_key, unprefixed_stream):
                        queue_name = q
                        break
                if not queue_name:
                    continue

                # Parse payload
                payload_bytes = fields.get(b"payload") or fields.get("payload")
                if not payload_bytes:
                    continue
                payload = json_loads(
                    payload_bytes if isinstance(payload_bytes, str) else payload_bytes.decode(),
                )

                delivery_tag = self._next_delivery_tag()
                payload.setdefault("properties", {})["delivery_tag"] = delivery_tag
                self._fanout_tags.add(delivery_tag)

                message = self._create_message(queue_name, payload, delivery_tag)
                await self._deliver_to_consumer(queue_name, message)
                return True

        return False

    # ---- message creation / delivery ---------------------------------------

    def _create_message(
        self,
        queue: str,
        payload: dict,
        delivery_tag: str,
    ) -> Message:
        """Create a Message from decoded payload dict."""
        body = payload.get("body", "")
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
        elif isinstance(body, (dict, list)):
            body = json_dumps(body).encode("utf-8")

        return Message(
            body=body,
            delivery_tag=delivery_tag,
            content_type=content_type,
            content_encoding=content_encoding,
            delivery_info={"exchange": "", "routing_key": queue},
            properties=properties,
            headers=headers,
            channel=self,
        )

    async def _deliver_to_consumer(
        self,
        queue: str,
        message: Message,
    ) -> None:
        """Find matching consumer and deliver message."""
        for q, callback, no_ack in self._consumers.values():
            if q == queue:
                if not no_ack:
                    self._delivered[message.delivery_tag] = (queue, message)

                try:
                    body = message.decode()
                except Exception:
                    body = message.body

                result = callback(body, message)
                if asyncio.iscoroutine(result):
                    await result
                return

    # ---- ack / reject / recover -------------------------------------------

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        if delivery_tag in self._fanout_tags:
            self._fanout_tags.discard(delivery_tag)
            self._delivered.pop(delivery_tag, None)
            return

        entry = self._delivered.pop(delivery_tag, None)
        if entry:
            queue, _ = entry
            # Atomic ack via Lua script (ZREM + DEL in one round-trip)
            script = await self._get_ack_script()
            await script(
                keys=[
                    self._messages_index_key(queue),
                    self._message_key(delivery_tag),
                ],
                args=[delivery_tag],
            )

    async def basic_reject(
        self,
        delivery_tag: str,
        requeue: bool = True,
    ) -> None:
        if delivery_tag in self._fanout_tags:
            self._fanout_tags.discard(delivery_tag)
            self._delivered.pop(delivery_tag, None)
            return

        entry = self._delivered.pop(delivery_tag, None)
        if entry:
            queue, _ = entry
            if requeue:
                await self._requeue_by_tag(delivery_tag, leftmost=True)
            else:
                # Atomic remove via Lua script
                script = await self._get_ack_script()
                await script(
                    keys=[
                        self._messages_index_key(queue),
                        self._message_key(delivery_tag),
                    ],
                    args=[delivery_tag],
                )

    async def basic_recover(self, requeue: bool = True) -> None:
        if requeue:
            for delivery_tag in list(self._delivered):
                if delivery_tag not in self._fanout_tags:
                    await self._requeue_by_tag(delivery_tag, leftmost=True)
        self._delivered.clear()
        self._fanout_tags.clear()

    async def _requeue_by_tag(
        self,
        delivery_tag: str,
        leftmost: bool = False,
    ) -> bool:
        """Requeue a rejected message to its queue using Lua script.

        The Lua script atomically reads the routing_key (queue) from the message
        hash, adds the message back to that queue with NX flag, and updates
        the messages_index with a new queue_at score.
        """
        message_key = self._message_key(delivery_tag)

        script = await self._get_requeue_script()
        result = await script(
            keys=[message_key],
            args=[
                1 if leftmost else 0,
                PRIORITY_SCORE_MULTIPLIER,
                self._message_ttl,
                self._global_keyprefix,
                QUEUE_KEY_PREFIX,
                MESSAGE_KEY_PREFIX,
                self._visibility_timeout,
                MESSAGES_INDEX_PREFIX,
            ],
        )
        return bool(result)

    # ---- periodic background tasks ----------------------------------------

    def _start_periodic_tasks(self) -> None:
        """Start background tasks if not already running."""
        if self._enqueue_task is None or self._enqueue_task.done():
            self._enqueue_task = asyncio.ensure_future(
                self._periodic_enqueue_due(),
            )
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.ensure_future(
                self._periodic_heartbeat(),
            )

    async def _periodic_enqueue_due(self) -> None:
        """Periodically enqueue delayed / timed-out messages."""
        while not self._closed:
            try:
                await asyncio.sleep(DEFAULT_REQUEUE_CHECK_INTERVAL)
                if self._closed:
                    break
                await self._enqueue_due_messages()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic enqueue")

    async def _periodic_heartbeat(self) -> None:
        """Periodically update message index scores (visibility heartbeat)."""
        interval = self._visibility_timeout / 3
        while not self._closed:
            try:
                await asyncio.sleep(interval)
                if self._closed:
                    break
                await self._update_messages_index()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic heartbeat")

    async def _periodic_refresh_expires(self) -> None:
        """Periodically refresh PEXPIRE on queues with x-expires."""
        if not self._expires:
            return
        min_ttl_ms = min(self._expires.values())
        interval = min_ttl_ms / 2 / 1000  # ms → s, ÷2
        while not self._closed:
            try:
                await asyncio.sleep(interval)
                if self._closed:
                    break
                await self._refresh_queue_expires()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic expires refresh")

    def _update_expires_task(self) -> None:
        """(Re)start the expires refresh task when TTL config changes."""
        if self._expires_task is not None and not self._expires_task.done():
            self._expires_task.cancel()
        if self._expires:
            self._expires_task = asyncio.ensure_future(
                self._periodic_refresh_expires(),
            )

    async def _enqueue_due_messages(self) -> tuple[int, int]:
        """Run Lua script to enqueue messages whose queue_at has passed.

        Returns (total_enqueued, total_dropped).
        """
        active_queues = list({q for q, _, _ in self._consumers.values() if q not in self.active_fanout_queues})
        if not active_queues:
            return 0, 0

        now = time()
        threshold = now + DEFAULT_REQUEUE_CHECK_INTERVAL
        total_enqueued = 0
        total_dropped = 0
        script = await self._get_enqueue_script()

        # Compute max_restore_count arg (-1 = no limit)
        max_rc = -1 if self._max_restore_count is None else self._max_restore_count

        for queue in active_queues:
            index_key = self._messages_index_key(queue)
            result = await script(
                keys=[index_key],
                args=[
                    threshold,
                    DEFAULT_REQUEUE_BATCH_LIMIT,
                    self._visibility_timeout,
                    PRIORITY_SCORE_MULTIPLIER,
                    MESSAGE_KEY_PREFIX,
                    self._global_keyprefix,
                    QUEUE_KEY_PREFIX,
                    max_rc,
                ],
            )
            if result:
                total_enqueued += int(result[0])
                total_dropped += int(result[1])

        if total_enqueued >= DEFAULT_REQUEUE_BATCH_LIMIT:
            logger.warning(
                "Enqueue hit batch limit %d. More messages may be waiting.",
                DEFAULT_REQUEUE_BATCH_LIMIT,
            )
        if total_dropped > 0:
            logger.warning(
                "Dropped %d messages exceeding max_restore_count=%s.",
                total_dropped,
                self._max_restore_count,
            )
        return total_enqueued, total_dropped

    async def _update_messages_index(self) -> None:
        """Update scores of delivered messages to prevent premature requeue."""
        if not self._delivered:
            return
        queue_at = time() + self._visibility_timeout + DEFAULT_REQUEUE_CHECK_INTERVAL
        async with self.client.pipeline(transaction=False) as pipe:
            for tag, (queue, _) in list(self._delivered.items()):
                if tag not in self._fanout_tags:
                    index_key = self._messages_index_key(queue)
                    # XX = only update if member already exists
                    await pipe.zadd(index_key, {tag: queue_at}, xx=True)
            await pipe.execute()

    async def _refresh_queue_expires(self) -> None:
        """Refresh PEXPIRE on queue + index keys."""
        if not self._expires:
            return
        async with self.client.pipeline(transaction=False) as pipe:
            for queue, ttl_ms in self._expires.items():
                await pipe.pexpire(self._queue_key(queue), ttl_ms)
                await pipe.pexpire(self._messages_index_key(queue), ttl_ms)
            await pipe.execute()

    # ---- get() and close() ------------------------------------------------

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        """Non-blocking single message fetch via atomic consume Lua script."""
        queue_key = self._queue_key(queue)
        new_queue_at = time() + self._visibility_timeout + DEFAULT_REQUEUE_CHECK_INTERVAL

        try:
            script = await self._get_consume_script()
            result = await script(
                keys=[queue_key],
                args=[
                    self._global_keyprefix,
                    MESSAGE_KEY_PREFIX,
                    str(new_queue_at),
                    MESSAGES_INDEX_PREFIX,
                    queue,
                ],
            )
        except Exception:
            logger.debug("Consume script error in get()", exc_info=True)
            result = None

        if not result:
            return None

        queue_name, delivery_tag, payload_json, restore_count = self._parse_consume_result(result)
        payload = json_loads(payload_json)
        if restore_count > 0:
            headers = payload.setdefault("headers", {})
            headers["x-restore-count"] = restore_count

        message = self._create_message(queue_name, payload, delivery_tag)
        if not no_ack:
            self._delivered[delivery_tag] = (queue_name, message)
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True

        # Let consumer iterations finish naturally — bounded by
        # `block_timeout`. They are never cancelled in the hot path, so any
        # in-flight FAST script / BZMPOP finalisation runs to completion and
        # the message is delivered rather than stranded in messages_index.
        # If an iteration is hung (Redis unresponsive), we cancel it after
        # `block_timeout + CLOSE_DRAIN_HEADROOM` as a last resort: stranding
        # at shutdown is acceptable since visibility-timeout restore recovers
        # those messages on the next worker startup.
        consumer_tasks = [t for t in (self._consume_iter_task, self._xread_iter_task) if t is not None and not t.done()]
        if consumer_tasks:
            drain_timeout = self._block_timeout + CLOSE_DRAIN_HEADROOM
            try:
                await asyncio.wait_for(
                    asyncio.gather(*consumer_tasks, return_exceptions=True),
                    timeout=drain_timeout,
                )
            except TimeoutError:
                logger.warning(
                    "consumer iterations did not drain within %.1fs at close "
                    "(block_timeout=%.1fs + headroom=%.1fs); cancelling — "
                    "in-flight messages may be stranded until visibility-"
                    "timeout restore",
                    drain_timeout,
                    self._block_timeout,
                    CLOSE_DRAIN_HEADROOM,
                )
                for task in consumer_tasks:
                    if not task.done():
                        task.cancel()
                for task in consumer_tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # fmt: skip
                        pass
        self._consume_iter_task = None
        self._xread_iter_task = None

        # Cancel periodic tasks
        for task in (self._enqueue_task, self._heartbeat_task, self._expires_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # fmt: skip
                    pass

        # Requeue unacked messages
        for delivery_tag, (queue, _) in list(self._delivered.items()):
            if delivery_tag not in self._fanout_tags:
                try:
                    await self._requeue_by_tag(delivery_tag, leftmost=True)
                except Exception:
                    logger.warning(
                        "Failed to requeue %s to %s",
                        delivery_tag,
                        queue,
                    )
        self._delivered.clear()
        self._fanout_tags.clear()

        # Delete auto-delete queues
        for queue in list(self.auto_delete_queues):
            try:
                await self.queue_delete(queue)
            except Exception:
                pass

        self._consumers.clear()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(BaseTransport):
    """Pure asyncio Valkey/Redis transport with priority queues, reliable fanout, and delayed delivery.

    Uses two clients:
    - Main client for BZMPOP, sorted set ops, hash ops, publish
    - Sub-client dedicated to XREAD BLOCK for fanout streams

    Supports both valkey-py and redis-py. The URL scheme selects which library
    to prefer (with fallback if only one is installed).
    """

    Channel = Channel
    default_port = 6379

    driver_type = "redis"
    driver_name = "redis"

    supports_native_delayed_delivery = True

    connection_errors = (
        BaseTransport.connection_errors
        + (
            ConnectionRefusedError,
            TimeoutError,
        )
        + _redis_connection_errors
    )

    channel_errors = BaseTransport.channel_errors + _redis_channel_errors

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        **options: Any,
    ) -> None:
        self._lib = resolve_lib(url)
        self._aiolib = resolve_async_lib(url)
        self._exc = resolve_exceptions(url)
        self.driver_name = self._lib.__name__

        super().__init__(url, **options)
        self._url = normalize_url(self._url, self._lib)
        self._client = None
        self._subclient = None
        self._channels: list[Channel] = []
        self._connection_id = str(uuid.uuid4())
        self._connected = False
        self._db = _parse_db_from_url(url)

    #: Transport-only options that should not be forwarded to the Redis client
    _TRANSPORT_ONLY_OPTIONS = frozenset(
        {
            "global_keyprefix",
            "visibility_timeout",
            "message_ttl",
            "stream_maxlen",
            "fanout_prefix",
            "max_restore_count",
            "credential_provider",
        },
    )

    def _client_kwargs(self) -> dict[str, Any]:
        """Filter transport-only options from client kwargs."""
        return {k: v for k, v in self._options.items() if k not in self._TRANSPORT_ONLY_OPTIONS}

    def _process_credential_provider(self) -> dict[str, Any]:
        """Process credential_provider option and return extra kwargs for Redis client.

        Accepts a CredentialProvider instance or a dotted import path string.
        When set, returns credential_provider kwarg (username/password in URL
        are still used by from_url but credential_provider takes precedence).
        """
        credential_provider = self._options.get("credential_provider")
        if credential_provider is None:
            return {}

        if isinstance(credential_provider, str):
            # Import dotted path
            from kombu.utils.imports import symbol_by_name

            credential_provider_cls = symbol_by_name(credential_provider)
            credential_provider = credential_provider_cls()

        # Validate using the resolved library's CredentialProvider
        try:
            CredentialProvider = self._lib.credentials.CredentialProvider
        except (AttributeError, ImportError):  # fmt: skip
            CredentialProvider = None  # type: ignore[assignment, misc]

        if CredentialProvider is not None and not isinstance(credential_provider, CredentialProvider):
            raise ValueError(
                "credential_provider must be an instance of "
                f"{CredentialProvider.__module__}.CredentialProvider (or a subclass)",
            )

        return {"credential_provider": credential_provider}

    async def connect(self) -> None:
        if self._connected:
            return

        client_kw = self._client_kwargs()
        cred_kw = self._process_credential_provider()
        client_kw.update(cred_kw)

        self._client = self._aiolib.from_url(
            self._url,
            decode_responses=False,
            **client_kw,
        )
        self._subclient = self._aiolib.from_url(
            self._url,
            decode_responses=False,
            **client_kw,
        )

        await self._client.ping()
        await self._subclient.ping()
        self._connected = True
        logger.debug("Connected via %s at %s (dual clients)", self._lib.__name__, self._url)

    async def close(self) -> None:
        for channel in self._channels:
            await channel.close()
        self._channels.clear()

        if self._subclient:
            await self._subclient.aclose()
            self._subclient = None

        if self._client:
            await self._client.aclose()
            self._client = None

        self._connected = False

    async def create_channel(self) -> Channel:
        if not self._connected:
            await self.connect()
        channel = Channel(self, self._connection_id)
        self._channels.append(channel)
        return channel

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def driver_version(self) -> str:
        try:
            return self._lib.__version__
        except AttributeError:
            return "N/A"
