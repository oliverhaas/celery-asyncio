"""Pure asyncio Valkey/Redis transport with priority queues, Streams fanout, and delayed delivery.

This transport uses valkey.asyncio or redis.asyncio for all operations and provides:
1. BZMPOP + sorted sets for regular queues — full 0-255 priority support
2. Streams for fanout exchanges — reliable, not lossy like PUB/SUB
3. Native delayed delivery — delay integrated into sorted set scoring
4. Per-message hash storage — reliability and visibility tracking
5. Global key prefixing — multi-tenant support
6. FAST/SLOW consume mode — atomic Lua script with BZMPOP fallback
7. Atomic ack — Lua script for ZREM+DEL atomicity
8. Delivery count tracking — delivery_limit enforcement

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
* ``requeue_check_interval``: Seconds between sweeps that restore timed-out and delayed
  messages (default: 60). It is also the grace margin added to each visibility deadline,
  so the worst-case restore delay is roughly ``visibility_timeout + 2 *
  requeue_check_interval``. Lower it alongside a low ``visibility_timeout``, which on its
  own cannot make restores happen sooner than the sweep.
* ``queue_expires``: Fallback ``x-expires`` in seconds for queues declared without one,
  which also puts the same TTL on fanout streams (default: None, queues live until deleted)
* ``message_ttl``: TTL for per-message hashes in seconds (-1 = no TTL)
* ``stream_maxlen``: Maximum stream length for fanout streams (default: 10000)
* ``fanout_prefix``: Prefix for fanout stream keys (default: '/{db}.')
* ``delivery_limit``: Max times a message may be delivered before it is dropped, as in
  RabbitMQ's quorum-queue ``delivery-limit`` policy (default: 20, None = no limit)
* ``block_timeout``: Server-side BZMPOP/XREAD BLOCK duration per consumer iteration in seconds (default: 10)
* ``credential_provider``: A CredentialProvider instance or dotted import path
* ``socket_timeout``: Socket timeout in seconds
* ``socket_connect_timeout``: Socket connection timeout in seconds
* ``health_check_interval``: Health check interval for connections
* ``max_connections``: Maximum connections in pool

Binding lifetime
================
``_kombu.binding.{exchange}`` is a sorted set scored with the unix time each binding goes
stale, which is ``x-expires`` after its last refresh (at least ``MIN_BINDING_LIFETIME``).
A queue without ``x-expires`` is scored ``+inf`` and its binding only ever goes away on an
explicit unbind. Declaring, refreshing and publishing all rescore; the publish path drops
whatever has aged out, so cleanup rides the read path and nothing has to sweep. The key is
a sorted set rather than the set kombu's own Redis transport writes, so the two can no
longer share it; the first bind converts an inherited set in place.
"""

import asyncio
import base64
import re
import urllib.parse
import uuid
from collections import deque
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, NamedTuple

from kombu.exceptions import InconsistencyError
from kombu.log import get_logger
from kombu.message import Message
from kombu.transport._valkey_redis_compat import (
    get_all_channel_errors,
    get_all_connection_errors,
    normalize_url,
    resolve_async_lib,
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


class IterationOutcome(NamedTuple):
    """What the consumer iterations that finished in one wait produced."""

    delivered: bool = False
    #: Whether one of them ran to completion with nothing to deliver. Only
    #: then is starting another worthwhile; one that failed or was cancelled
    #: hands control back to the caller instead.
    exhausted: bool = False


class SweepStats(NamedTuple):
    """What one requeue sweep did, summed over the queues it visited."""

    enqueued: int = 0
    dropped: int = 0
    redelivered: int = 0
    orphaned: int = 0


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
DEFAULT_STREAM_MAXLEN = 10000
DEFAULT_REQUEUE_CHECK_INTERVAL = 60
DEFAULT_REQUEUE_BATCH_LIMIT = 1000
# Ceiling on one consume batch, so a large prefetch_count cannot make a single
# script run hold the Redis event loop for long.
MAX_CONSUME_BATCH = 100
DEFAULT_MESSAGE_TTL = -1
MIN_QUEUE_EXPIRES = 10_000
# Fallback x-expires in seconds for queues declared without one. None keeps
# kombu semantics, where a queue lives until something deletes it.
DEFAULT_QUEUE_EXPIRES: int | None = None
# Matches RabbitMQ, which has applied a delivery-limit of 20 to quorum queues
# since 4.0. None disables the limit and lets a poison message redeliver forever.
DEFAULT_DELIVERY_LIMIT: int | None = 20

# Cap on how many dropped messages a single sweep names per queue in the error
# log. The drop deletes the hash, so that line is the message's last trace.
DROPPED_REPORT_LIMIT = 10

# Floor under how long a binding survives without a refresh, in seconds.
# Bindings are scored with their queue's x-expires, but the processes leaving
# them behind cannot refresh: a celery control client has no event loop, and its
# reply queue's 10s x-expires is shorter than the call the binding must outlive.
MIN_BINDING_LIFETIME = 300

# Default server-side block duration for BZMPOP/XREAD inside a single consumer
# iteration. Overridable via the `block_timeout` transport option.
DEFAULT_BLOCK_TIMEOUT = 10.0

# How much longer than `block_timeout` a socket read is given before it is
# treated as a timeout. The client libraries read every reply under a socket
# timeout of their own and pass no separate deadline for a blocking command,
# so a socket timeout shorter than the block aborts every blocking consume.
SOCKET_TIMEOUT_HEADROOM = 5.0

# How long past its own `block_timeout` a consumer iteration may stay pending
# before it is reported as stalled. Redis returns within BLOCK, so anything
# beyond this means the client connection or the server has hung.
CONSUMER_STALL_HEADROOM = 30.0

# How long drain_events waits when there is nothing to wait for, so a caller
# that ignores the return value cannot spin. Never longer than the timeout the
# caller asked for.
IDLE_POLL_INTERVAL = 0.1

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
_CONVERT_BINDINGS_LUA = (_PACKAGE_DIR / "transport_convert_bindings.lua").read_text()

# ---------------------------------------------------------------------------
# Valkey/Redis error tuples (from ALL installed libraries for catch-all)
# ---------------------------------------------------------------------------

_redis_connection_errors = get_all_connection_errors()
_redis_channel_errors = get_all_channel_errors()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_wrongtype(exc: Exception) -> bool:
    """Whether the server rejected a command because the key holds another type.

    Substring, not prefix: a pipeline reply arrives wrapped as
    "Command # 1 (ZRANGEBYSCORE ...) of pipeline caused error: WRONGTYPE ...".
    """
    return "WRONGTYPE" in str(exc)


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
    """Match a routing key against an AMQP topic pattern.

    ``*`` stands for exactly one word and ``#`` for zero or more words. Every
    other character is literal: a routing key or pattern may contain regex
    metacharacters, so both are escaped word by word rather than substituted
    into a regex whole.
    """
    words = pattern.split(".")
    if words == ["#"]:
        return True

    parts: list[str] = []
    separator_pending = False
    for index, word in enumerate(words):
        if word == "#":
            if index == 0:
                # Leading zero or more words, each followed by its separator.
                parts.append(r"(?:[^.]+\.)*")
                separator_pending = False
            else:
                # The hash swallows the separator in front of it, so the words
                # it stands for may also be none at all.
                parts.append(r"(?:\.[^.]+)*")
                separator_pending = index < len(words) - 1
            continue
        if separator_pending:
            parts.append(r"\.")
        parts.append(r"[^.]+" if word == "*" else re.escape(word))
        separator_pending = True
    return re.fullmatch("".join(parts), routing_key) is not None


def _parse_db_from_url(url: str) -> str:
    """Extract database number from Redis URL."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    return path or "0"


def _duration_option(opts: dict, name: str, default: float) -> float:
    """Read a duration transport option, in seconds.

    Zero and negative durations have no meaning for any of them and would turn
    a wait into a busy loop or put a deadline in the past, so they are refused
    at channel creation rather than misbehaving later.
    """
    value = opts.get(name, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):  # fmt: skip
        raise TypeError(f"{name} must be a number of seconds, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0 seconds, got {value!r}")
    return float(value)


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
    _warned_queue_expires_clamp = False

    # Lua script handles. Registered on first use; the accessor's assignment
    # shadows the class default, so each channel keeps its own handle.
    _enqueue_script = _requeue_script = _consume_script = _ack_script = None
    _convert_bindings_script = None

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._channel_id = str(uuid.uuid4())
        self._closed = False

        # Consumer state: tag → (queue, callback, no_ack)
        self._consumers: dict[str, tuple[str, Callable, bool]] = {}
        self.no_ack_consumers: set[str] | None = set()
        # Queues whose consumers never ack, so a delivery from one is finished
        # the moment it is popped. Consulted by the consume Lua script.
        self._no_ack_queues: set[str] = set()

        # Exchange / binding state
        self._exchanges: dict[str, dict] = {}
        # queue → {(exchange, member)} for the bindings this channel declared,
        # which are the only ones it may rescore on refresh or publish.
        self._binding_members: dict[str, set[tuple[str, str]]] = {}

        # Fanout state
        self._fanout_queues: dict[str, tuple[str, str]] = {}  # queue → (exchange, rk)
        self.active_fanout_queues: set[str] = set()
        self.auto_delete_queues: set[str] = set()
        self._stream_offsets: dict[str, str] = {}  # stream_key → last ID

        # Message tracking
        self._delivered: dict[str, tuple[str, Message]] = {}  # tag → (queue, msg)
        self._fanout_tags: set[str] = set()
        self._prefetch_count = 0
        self._prefetch_buffer: deque[tuple[str, str, str, int]] = deque()
        self._delivery_tag_counter = 0

        # Per-queue TTL state
        self._expires: dict[str, int] = {}  # queue → TTL ms
        self._message_ttls: dict[str, int] = {}  # queue → message TTL ms

        # Config from transport options
        opts = transport._options
        self._global_keyprefix: str = opts.get("global_keyprefix", "")
        self._visibility_timeout: float = _duration_option(
            opts,
            "visibility_timeout",
            DEFAULT_VISIBILITY_TIMEOUT,
        )
        # Both how often timed-out and delayed messages are restored and the
        # grace margin added to every visibility deadline, so nothing becomes
        # eligible for restore before the sweep that would pick it up has run.
        # It is configurable because a low visibility_timeout otherwise looks
        # ignored: the real wait is bounded by the sweep, not by the timeout
        # (upstream kombu 9ee8595b).
        self._requeue_check_interval: float = _duration_option(
            opts,
            "requeue_check_interval",
            DEFAULT_REQUEUE_CHECK_INTERVAL,
        )
        self._message_ttl: int = opts.get("message_ttl", DEFAULT_MESSAGE_TTL)
        self._queue_expires: int | None = opts.get("queue_expires", DEFAULT_QUEUE_EXPIRES)
        self._stream_maxlen: int = opts.get("stream_maxlen", DEFAULT_STREAM_MAXLEN)
        self._delivery_limit: int | None = opts.get(
            "delivery_limit",
            DEFAULT_DELIVERY_LIMIT,
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
        # iteration, and the ceiling on how long one drain_events wait blocks
        # in Redis. Zero would spin.
        self._block_timeout: float = _duration_option(opts, "block_timeout", DEFAULT_BLOCK_TIMEOUT)

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

    def _global_expires_ms(self) -> int | None:
        """The queue_expires option in milliseconds, floored like x-expires."""
        if self._queue_expires is None:
            return None
        expires_ms = int(self._queue_expires * 1000)
        if expires_ms < MIN_QUEUE_EXPIRES:
            if not self._warned_queue_expires_clamp:
                logger.warning(
                    "queue_expires %dms is below minimum %dms, clamping.",
                    expires_ms,
                    MIN_QUEUE_EXPIRES,
                )
                Channel._warned_queue_expires_clamp = True
            expires_ms = MIN_QUEUE_EXPIRES
        return expires_ms

    # ---- binding lifetime --------------------------------------------------

    @staticmethod
    def _binding_member(routing_key: str, queue: str) -> str:
        return BINDING_SEP.join([routing_key or "", routing_key or "", queue or ""])

    def _binding_stale_at(self, queue: str, now: float | None = None) -> float:
        """Unix time the bindings of this queue go stale.

        Only a queue that expires can leave a binding behind that nobody wants:
        one that stays around has to keep its route, so it is scored +inf and
        goes away on an explicit unbind or not at all.

        The window never drops below MIN_BINDING_LIFETIME, because the processes
        that abandon bindings are the ones that cannot refresh them. A celery
        control client has no event loop, and the 10s x-expires on its reply
        queue is shorter than the control call the binding has to outlive.
        """
        expires_ms = self._expires.get(queue)
        if expires_ms is None:
            return float("inf")
        return (time() if now is None else now) + max(expires_ms / 1000, MIN_BINDING_LIFETIME)

    def _binding_ttl_ms(self, queue: str) -> int | None:
        """TTL to put on a binding key touched on behalf of this queue.

        Only with the global queue_expires option: a per-queue x-expires alone
        must not put a TTL on a table that may also hold the bindings of queues
        that never expire. The floor mirrors _binding_stale_at, so the key
        outlives its own members' staleness deadlines.
        """
        global_ms = self._global_expires_ms()
        if global_ms is None:
            return None
        return max(self._expires.get(queue, global_ms), MIN_BINDING_LIFETIME * 1000)

    async def _touch_binding_key(self, exchange: str, queue: str) -> None:
        """Give the binding key a TTL when the global queue_expires option is on.

        GT, so a queue with a short window cannot shrink a TTL that another
        queue's touch pushed further out. GT treats a key without a TTL as
        infinite and declines, so a key that has none yet gets one directly.
        """
        ttl_ms = self._binding_ttl_ms(queue)
        if ttl_ms is None:
            return
        key = self._binding_key(exchange)
        if not await self.client.pexpire(key, ttl_ms, gt=True) and await self.client.pttl(key) == -1:
            await self.client.pexpire(key, ttl_ms)

    async def _convert_binding_set(self, exchange: str) -> None:
        """Turn a binding table left behind as a plain set into a sorted set."""
        script = await self._get_convert_bindings_script()
        converted = await script(keys=[self._binding_key(exchange)])
        logger.info(
            "Converted the binding table of exchange %r from a set to a sorted set, "
            "carrying over %s member(s) with no staleness deadline",
            exchange,
            converted,
        )

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

    async def _get_convert_bindings_script(self):
        if self._convert_bindings_script is None:
            self._convert_bindings_script = self.client.register_script(
                _CONVERT_BINDINGS_LUA,
            )
        return self._convert_bindings_script

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
        await self.client.delete(self._binding_key(exchange))

    # ---- queue operations --------------------------------------------------

    async def declare_queue(self, queue: Queue) -> str:
        name = queue.name or f"amq.gen-{uuid.uuid4()}"
        queue.name = name

        # Parse queue arguments for TTL
        arguments = getattr(queue, "queue_arguments", None) or {}
        if not arguments:
            arguments = getattr(queue, "arguments", None) or {}

        # Recomputed on every declare, never skipped when the queue is already
        # known: a redeclare is how a caller changes or drops a TTL, and the old
        # "first declare wins" rule silently kept a stale one forever.
        x_expires = arguments.get("x-expires")
        if x_expires is None:
            x_expires = self._global_expires_ms()
        else:
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
        previous_expires = self._expires.get(name)
        if x_expires is None:
            self._expires.pop(name, None)
        else:
            self._expires[name] = x_expires
        if x_expires != previous_expires:
            # The refresh interval is derived from the smallest TTL, so it has
            # to be recomputed whenever the set of TTLs changes.
            self._update_expires_task()

        x_message_ttl = arguments.get("x-message-ttl")
        if x_message_ttl is None:
            self._message_ttls.pop(name, None)
        else:
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
        # Detect fanout
        exchange_meta = self._exchanges.get(exchange, {})
        if exchange_meta.get("type") == "fanout":
            self._fanout_queues[queue] = (exchange, routing_key.replace("#", "*"))
            # Fanout delivery is one XADD to the stream and never consults the
            # table, yet every worker binds its own amq.gen-* queues to it, so
            # the table only grows: one dead member per worker that ever ran.
            # Drop what earlier versions accumulated.
            await self.client.delete(self._binding_key(exchange))
            return

        member = self._binding_member(routing_key, queue)
        self._binding_members.setdefault(queue, set()).add((exchange, member))
        key = self._binding_key(exchange)
        try:
            await self.client.zadd(key, {member: self._binding_stale_at(queue)})
        except _redis_channel_errors as exc:
            if not _is_wrongtype(exc):
                raise
            await self._convert_binding_set(exchange)
            await self.client.zadd(key, {member: self._binding_stale_at(queue)})
        await self._touch_binding_key(exchange, queue)

    async def queue_unbind(
        self,
        queue: str,
        exchange: str,
        routing_key: str = "",
        arguments: dict | None = None,
    ) -> None:
        if self._exchanges.get(exchange, {}).get("type") == "fanout":
            # Nothing was written for it, so there is nothing to remove.
            self._fanout_queues.pop(queue, None)
            self.active_fanout_queues.discard(queue)
            return

        member = self._binding_member(routing_key, queue)
        declared = self._binding_members.get(queue)
        if declared is not None:
            declared.discard((exchange, member))
            if not declared:
                del self._binding_members[queue]
        try:
            await self.client.zrem(self._binding_key(exchange), member)
        except _redis_channel_errors as exc:
            if not _is_wrongtype(exc):
                raise
            # Table still a plain set from an older deployment. Unbinding is
            # no reason to convert it, so just remove the member in place.
            await self.client.srem(self._binding_key(exchange), member)

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
        self._binding_members.pop(queue, None)

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

    def _exchange_is_durable(self, exchange: str) -> bool:
        """Whether the exchange was declared durable.

        An exchange this channel never declared has no state entry. Assume
        durable then, which keeps the raise-and-redeclare path the default for
        exchanges whose bindings are supposed to outlive their consumers.
        """
        entry = self._exchanges.get(exchange)
        if not entry:
            return True
        return bool(entry.get("durable", True))

    async def _read_bindings(self, exchange: str) -> Any:
        """Read the live bindings of an exchange, dropping the ones that aged out.

        Pruning rides the read path because nothing else can reach these members:
        a binding is only ever unbound by the process that declared it, and the
        ones that pile up are precisely the ones whose process is gone. The
        removal costs no extra round trip, and in steady state it removes
        nothing, so Redis has nothing to propagate.

        The pruned members are read back first and logged: dropping a binding
        silently reroutes messages, so the log line is the only way to tell an
        aged-out route from one that never existed.
        """
        key = self._binding_key(exchange)
        now = time()
        try:
            # Not a transaction: this runs on the publish path, and a bind
            # landing between the commands is indistinguishable from one
            # landing just after the read.
            async with self.client.pipeline(transaction=False) as pipe:
                await pipe.zrangebyscore(key, "-inf", now)
                await pipe.zremrangebyscore(key, "-inf", now)
                await pipe.zrange(key, 0, -1)
                stale, _removed, live = await pipe.execute()
        except _redis_channel_errors as exc:
            if not _is_wrongtype(exc):
                raise
            # Table still a plain set from an older deployment or from kombu's
            # own Redis transport. Readable as it is; the next bind converts it.
            return await self.client.smembers(key)
        if stale:
            logger.info(
                "Exchange %r: dropped %d abandoned binding(s): %s",
                exchange,
                len(stale),
                ", ".join(sorted(m.decode() if isinstance(m, bytes) else m for m in stale)),
            )
        return live

    async def _load_bindings(self, exchange: str) -> list[tuple[str, str]]:
        """Load bindings from Redis, supporting both sep and JSON formats."""
        members = await self._read_bindings(exchange)
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
            if not bindings:
                # An empty table is inconsistent state here, not nowhere to go:
                # a direct binding is known by name (topic and fanout may empty
                # legitimately). InconsistencyError is a connection error, so
                # Connection.ensure redeclares rather than lose the publish.
                if not self._exchange_is_durable(exchange):
                    # A transient direct exchange empties by design once its
                    # consumers leave (pidbox reply exchanges do this), and
                    # redeclaring cannot recreate someone else's binding, so
                    # retrying would only churn.
                    logger.info(
                        "Dropped message to transient exchange %r with routing key %r: binding table is empty.",
                        exchange,
                        routing_key,
                    )
                    return
                key = self._binding_key(exchange)
                raise InconsistencyError(
                    f"Cannot route to {exchange}: no bindings declared."
                    f" Probably the key {key!r} has been removed from the database,"
                    f" or every binding in it went stale.",
                )
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
        expires_ms = self._global_expires_ms()
        if expires_ms is not None:
            # Refreshed by the publisher, because only a publisher can say the
            # exchange is still in use. A consumer that reconnects after the
            # stream expired resumes from "$" and has missed nothing: the
            # stream can only have expired if nobody published for that long.
            await self.client.pexpire(stream_key, expires_ms)

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
        payload: dict[str, Any]
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
        is_native_delayed = eta_timestamp is not None and (float(eta_timestamp) - now) > self._requeue_check_interval
        if is_native_delayed:
            eta_timestamp = float(eta_timestamp)  # type: ignore[arg-type]
        visible_at = eta_timestamp if is_native_delayed else now

        queue_score = _queue_score(priority, visible_at)
        # queue_at = time when enqueue_due_messages will pick up this message.
        # Adding RCI ensures the message won't be restored prematurely before
        # the next enqueue cycle runs.
        queue_at = eta_timestamp if is_native_delayed else now + self._visibility_timeout + self._requeue_check_interval

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
                    "native_delayed": 1 if is_native_delayed else 0,
                    "eta": eta_timestamp or 0,
                    "delivery_count": 0,
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
                # Publishing keeps the route alive (producers run no refresh
                # timer); GT never pulls back another channel's longer deadline.
                stale_at = self._binding_stale_at(queue, now=now)
                binding_ttl_ms = self._binding_ttl_ms(queue)
                for exchange, member in self._binding_members.get(queue, ()):
                    binding_key = self._binding_key(exchange)
                    await pipe.zadd(binding_key, {member: stale_at}, gt=True)
                    if binding_ttl_ms is not None:
                        await pipe.pexpire(binding_key, binding_ttl_ms, gt=True)

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

        if no_ack:
            self._no_ack_queues.add(queue)
            if self.no_ack_consumers is not None:
                self.no_ack_consumers.add(consumer_tag)

        if queue in self._fanout_queues:
            self.active_fanout_queues.add(queue)

        self._start_periodic_tasks()
        return consumer_tag

    async def basic_cancel(self, consumer_tag: str) -> None:
        entry = self._consumers.pop(consumer_tag, None)
        if entry:
            queue, _, _ = entry
            # Recomputed rather than discarded: another consumer may still be
            # reading the same queue with no_ack set.
            if not any(q == queue and na for q, _cb, na in self._consumers.values()):
                self._no_ack_queues.discard(queue)
            self.active_fanout_queues.discard(queue)
            if not any(q == queue for q, _cb, _na in self._consumers.values()):
                await self._restore_prefetch_buffer(queue)
        if self.no_ack_consumers is not None:
            self.no_ack_consumers.discard(consumer_tag)

    # ---- drain_events (FAST/SLOW consume) ----------------------------------

    async def drain_events(self, timeout: float | None = None) -> bool:
        """Deliver at most one message to one of this channel's consumers.

        The call returns within ``timeout`` seconds, which is what the celery
        worker loop needs to fire an ETA whose deadline is closer than the
        transport's own block duration. ``timeout=0`` polls what is already
        available and never blocks; ``timeout=None`` runs a single consume
        iteration bounded by ``block_timeout``.

        The blocking wait rides on consumer iteration tasks, each one a single
        FAST/SLOW (or XREAD) cycle. They are never cancelled here, because a
        cancel mid-script or post-BZMPOP strands the message that was about to
        be delivered; a call that runs out of time leaves its iteration
        pending and the next call waits on the same one.

        A broker failure is raised, not reported as an idle queue, so the
        consumer can reconnect.
        """
        if timeout is not None and timeout <= 0:
            return await self._poll_ready()

        if self._closing or not self._consumers:
            await self._idle_sleep(timeout)
            return False

        loop = asyncio.get_running_loop()
        return await self._drain_until(None if timeout is None else loop.time() + timeout)

    async def _drain_until(self, deadline: float | None) -> bool:
        """Run consumer iterations until one delivers or the deadline passes.

        A ``deadline`` of ``None`` means a single iteration.
        """
        loop = asyncio.get_running_loop()
        while True:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False

            # An earlier call may have run out of time and left its iteration
            # running. Collect it before starting anything, so its result is
            # not dropped on the floor along with any failure it carries.
            finished = {t for t in (self._consume_iter_task, self._xread_iter_task) if t is not None and t.done()}
            if finished and self._collect_iterations(finished).delivered:
                return True

            self._ensure_consumer_tasks(remaining)
            tasks = [t for t in (self._consume_iter_task, self._xread_iter_task) if t is not None]
            if not tasks:
                await self._idle_sleep(remaining)
                return False

            # With no deadline of its own the wait rides on the iteration's
            # block; the backstop is only there for a client or server that
            # has stopped answering altogether.
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self._block_timeout + CONSUMER_STALL_HEADROOM if remaining is None else remaining,
            )

            # Per-task stall warning. asyncio.wait with FIRST_COMPLETED
            # returns as soon as ANY task completes, so a single hung
            # iteration alongside a healthy one would otherwise be silent.
            self._warn_stalled_iterations()

            if not done:
                return False
            outcome = self._collect_iterations(done)
            if outcome.delivered:
                return True
            # An iteration that came back empty. Start another one if the
            # caller still has time to spare. One that failed or was cancelled
            # returns straight away, so a broker that is down is reported
            # rather than retried for the rest of the window.
            if not outcome.exhausted or deadline is None:
                return False

    async def _idle_sleep(self, timeout: float | None) -> None:
        """Wait out a call that has nothing to wait on, without spinning."""
        await asyncio.sleep(IDLE_POLL_INTERVAL if timeout is None else min(timeout, IDLE_POLL_INTERVAL))

    async def _poll_ready(self) -> bool:
        """Deliver whatever is already available, without blocking."""
        if self._closing:
            return False
        regular_queues = self._regular_queues()
        if regular_queues and await self._fast_consume(regular_queues):
            return True
        # Only when no blocking XREAD is outstanding: both reads would start
        # from the same stream offset and deliver the same message twice.
        if self.active_fanout_queues and (self._xread_iter_task is None or self._xread_iter_task.done()):
            return await self._xread_wait(0)
        return False

    def _collect_iterations(self, done: AbstractSet[asyncio.Task]) -> IterationOutcome:
        """Read the results of the iterations that finished.

        A task that ends cancelled was cancelled by ``close()``, not by this
        caller, so its ``CancelledError`` stays here: re-raising it would
        cancel a caller that nobody asked to cancel. A broker failure does
        travel out, so the consumer can reconnect rather than sit on a dead
        socket reporting an empty queue.
        """
        delivered = exhausted = False
        failure: BaseException | None = None
        for task in done:
            if task is self._consume_iter_task:
                self._consume_iter_task = None
            elif task is self._xread_iter_task:
                self._xread_iter_task = None
            if task.cancelled():
                continue
            error = task.exception()
            if error is None:
                if task.result():
                    delivered = True
                else:
                    exhausted = True
            elif failure is None:
                failure = error
            else:
                # Both iterations failed in the same wait. Only one can be
                # raised, so the other is reported here.
                logger.warning("Consumer iteration failed.", exc_info=error)
        if failure is not None:
            raise failure
        return IterationOutcome(delivered=delivered, exhausted=exhausted)

    def _regular_queues(self) -> list[str]:
        return list(
            dict.fromkeys(q for q, _cb, _no_ack in self._consumers.values() if q not in self.active_fanout_queues),
        )

    def _ensure_consumer_tasks(self, remaining: float | None) -> None:
        """Start the iterations that are not running, bounded by `remaining`.

        A new iteration never blocks longer than the caller is prepared to
        wait, so drain_events can honour its timeout without cancelling
        anything. `block_timeout` stays the ceiling.
        """
        if self._closing:
            return
        block = self._block_timeout if remaining is None else min(remaining, self._block_timeout)
        loop = asyncio.get_running_loop()
        regular_queues = self._regular_queues()
        if regular_queues and (self._consume_iter_task is None or self._consume_iter_task.done()):
            self._consume_iter_task = asyncio.create_task(
                self._consume_regular(regular_queues, block),
            )
            self._consume_iter_started_at = loop.time()
            self._consume_iter_stall_warned = False
        if self.active_fanout_queues and (self._xread_iter_task is None or self._xread_iter_task.done()):
            self._xread_iter_task = asyncio.create_task(
                self._xread_wait(block),
            )
            self._xread_iter_started_at = loop.time()
            self._xread_iter_stall_warned = False

    def _warn_stalled_iterations(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        threshold = self._block_timeout + CONSUMER_STALL_HEADROOM
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

    async def _consume_regular(self, queues: list[str], timeout: float) -> bool:
        """Consume from regular queues using FAST/SLOW mode.

        FAST is the non-blocking atomic Lua script (ZPOPMIN + ZADD index +
        HMGET), SLOW the blocking BZMPOP it falls back to once the script
        reports every queue empty.
        """
        if self._consume_fast_mode:
            if await self._fast_consume(queues):
                return True
            # FAST returned nil, so all queues are empty: switch to SLOW.
            self._consume_fast_mode = False

        delivered = await self._slow_consume(queues, timeout)
        if delivered:
            self._consume_fast_mode = True  # Switch back to FAST
        return delivered

    async def basic_qos(self, prefetch_count: int = 0) -> None:
        """Set how many messages one consume round-trip may claim.

        Redis cannot push, so this is a fetch batch size rather than AMQP's cap
        on unacknowledged messages: the consume script claims up to this many
        messages at once and they are handed out from a local buffer, saving a
        round-trip each. It does not bound how many messages go unacknowledged,
        which stays a matter for the consumer; what it bounds is the buffer,
        since a batch is fetched only once the previous one has been handed out.
        Claimed messages carry their visibility deadline from the moment the
        script pops them, so a worker that dies holding a full buffer loses
        nothing.
        """
        self._prefetch_count = max(int(prefetch_count), 0)

    async def _fast_consume(self, queues: list[str]) -> bool:
        """FAST mode: atomic Lua script for non-blocking consume."""
        # Looping, so one undeliverable message cannot strand the rest behind it.
        while self._prefetch_buffer:
            if await self._deliver_claimed(*self._prefetch_buffer.popleft()):
                return True

        batch = max(min(self._prefetch_count, MAX_CONSUME_BATCH), 1)
        queue_keys = [self._queue_key(q) for q in queues]
        new_queue_at = time() + self._visibility_timeout + self._requeue_check_interval

        script = await self._get_consume_script()
        result = await script(
            keys=queue_keys,
            args=[
                self._global_keyprefix,
                MESSAGE_KEY_PREFIX,
                str(new_queue_at),
                MESSAGES_INDEX_PREFIX,
                str(batch),
                *queues,
                *("1" if q in self._no_ack_queues else "0" for q in queues),
            ],
        )

        if not result:
            return False

        claimed = [self._parse_consume_result(result[i : i + 4]) for i in range(0, len(result), 4)]
        self._prefetch_buffer.extend(claimed[1:])
        return await self._deliver_claimed(*claimed[0])

    async def _deliver_claimed(
        self,
        queue_name: str,
        delivery_tag: str,
        payload_json: str,
        delivery_count: int,
    ) -> bool:
        payload = json_loads(payload_json)
        message = self._create_message(queue_name, payload, delivery_tag, delivery_count)
        try:
            delivered = await self._deliver_to_consumer(queue_name, message)
        except BaseException:
            # The Lua script already popped this tag from the queue ZSET, so a
            # cancellation here (close() running out of headroom, say) would
            # leave the message invisible until the visibility timeout. Put it
            # straight back instead, at its original score and without
            # counting a redelivery: nobody has seen it. A callback that
            # raises is a different matter and is dealt with in
            # _deliver_to_consumer.
            self._delivered.pop(delivery_tag, None)
            await self._restore_to_queue(queue_name, delivery_tag)
            raise
        if not delivered:
            await self._restore_undeliverable(queue_name, delivery_tag)
        return delivered

    async def _restore_to_queue(
        self,
        queue: str,
        delivery_tag: str,
        score: float | None = None,
    ) -> None:
        """Put a popped tag back on the queue ZSET. Best-effort recovery."""
        await self._zadd_restore(queue, delivery_tag, score)

    async def _restore_undeliverable(
        self,
        queue: str,
        delivery_tag: str,
        score: float | None = None,
    ) -> None:
        """Put back a message popped for a queue that no longer has a consumer.

        Unlike `_restore_to_queue` this is not a cancellation path, so it is
        worth a warning: it means a `basic_cancel` raced an in-flight consume.
        A `no_ack` consumer is the one case that cannot be recovered, because
        the consume script already deleted the message hash; the tag restored
        here is then dangling and the next consume drops it.
        """
        logger.warning(
            "No consumer for %s when message %s was delivered; restoring it. "
            "A basic_cancel raced an in-flight consume.",
            queue,
            delivery_tag,
        )
        self._delivered.pop(delivery_tag, None)
        await self._zadd_restore(queue, delivery_tag, score)

    async def _zadd_restore(
        self,
        queue: str,
        delivery_tag: str,
        score: float | None = None,
    ) -> None:
        queue_key = self._queue_key(queue)
        if score is None:
            score = _queue_score(DEFAULT_PRIORITY, time())
        try:
            await self.client.zadd(queue_key, {delivery_tag: score})
        except Exception:
            # Reported rather than raised so the original cancellation is not
            # masked. The message stays in messages_index and the visibility
            # sweep re-enqueues it, so it is delayed rather than lost.
            logger.warning(
                "Could not restore message %s to queue %r.",
                delivery_tag,
                queue,
                exc_info=True,
            )

    async def _slow_consume(self, queues: list[str], timeout: float) -> bool:
        """SLOW mode: blocking BZMPOP with pipeline index refresh + HMGET."""
        queue_keys = [self._queue_key(q) for q in queues]

        result = await self.client.bzmpop(
            timeout,
            len(queue_keys),
            queue_keys,
            min=True,
        )

        if not result:
            return False

        queue_key_raw, members = result
        queue_key = queue_key_raw.decode() if isinstance(queue_key_raw, bytes) else queue_key_raw
        queue_key = self._unprefixed(queue_key)
        queue = queue_key.removeprefix(QUEUE_KEY_PREFIX)

        delivery_tag_raw, score_raw = members[0]
        delivery_tag = delivery_tag_raw.decode() if isinstance(delivery_tag_raw, bytes) else delivery_tag_raw
        original_score = float(score_raw)

        # BZMPOP popped this tag server-side. From here on we either deliver
        # it or push it back, even on cancellation, so the message isn't
        # stuck in messages_index for the visibility-timeout window.
        delivered = await self._claim_and_deliver(queue, delivery_tag, original_score)
        if delivered is None:
            return await self._drain_expired_and_deliver(queue)
        return delivered

    async def _claim_and_deliver(
        self,
        queue: str,
        delivery_tag: str,
        original_score: float,
    ) -> bool | None:
        """Take ownership of a popped tag and hand its message to a consumer.

        Returns None when the message hash is gone, which leaves the caller to
        move on to the next tag.
        """
        message_key = self._message_key(delivery_tag)
        index_key = self._messages_index_key(queue)
        new_queue_at = time() + self._visibility_timeout + self._requeue_check_interval

        no_ack = queue in self._no_ack_queues

        try:
            async with self.client.pipeline(transaction=False) as pipe:
                if no_ack:
                    # Nothing will ever ack this delivery, so an index entry
                    # would leak and the next sweep would redeliver. Mirrors the
                    # no_ack branch of the consume Lua script.
                    await pipe.zrem(index_key, delivery_tag)
                else:
                    # Not xx=True: a delivery with no index entry is tracked by
                    # nothing and lost on a worker crash. Without a transaction
                    # this can revive an entry for a just-acked message, which
                    # the empty-payload branch below ZREMs again.
                    await pipe.zadd(index_key, {delivery_tag: new_queue_at})
                await pipe.hmget(message_key, "payload", "delivery_count")
                if no_ack:
                    await pipe.delete(message_key)
                results = await pipe.execute()
        except BaseException:
            await self._restore_to_queue(queue, delivery_tag, original_score)
            raise

        payload_json = results[1][0]
        if not payload_json:
            await self.client.zrem(index_key, delivery_tag)
            return None

        payload = json_loads(payload_json)
        delivery_count = int(results[1][1] or 0)
        message = self._create_message(queue, payload, delivery_tag, delivery_count)
        try:
            delivered = await self._deliver_to_consumer(queue, message)
        except BaseException:
            self._delivered.pop(delivery_tag, None)
            await self._restore_to_queue(queue, delivery_tag, original_score)
            raise
        if not delivered:
            await self._restore_undeliverable(queue, delivery_tag, original_score)
        return delivered

    def _parse_consume_result(self, result: list) -> tuple[str, str, str, int]:
        """Parse the result from consume_message Lua script.

        Returns (queue_name, delivery_tag, payload_json, delivery_count).
        """
        queue_name = result[0].decode() if isinstance(result[0], bytes) else result[0]
        delivery_tag = result[1].decode() if isinstance(result[1], bytes) else result[1]
        payload_json = result[2].decode() if isinstance(result[2], bytes) else result[2]
        delivery_count = int(result[3] or 0)
        return queue_name, delivery_tag, payload_json, delivery_count

    async def _drain_expired_and_deliver(self, queue: str) -> bool:
        """Pop tags until one still has its message, or the queue runs dry.

        A message whose hash has expired leaves its tag behind on the queue
        ZSET. Delivery goes through the same claim path as the BZMPOP one, so
        a message handed out here is tracked in ``messages_index`` and comes
        back after the visibility timeout if it is never acked.
        """
        queue_key = self._queue_key(queue)
        while True:
            popped = await self.client.zpopmin(queue_key, count=1)
            if not popped:
                return False
            delivery_tag_raw, score = popped[0]
            delivery_tag = delivery_tag_raw.decode() if isinstance(delivery_tag_raw, bytes) else delivery_tag_raw
            delivered = await self._claim_and_deliver(queue, delivery_tag, float(score))
            if delivered is not None:
                return delivered

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

        # block=None is a non-blocking read; block=0 would block forever.
        result = await self.subclient.xread(
            streams,
            count=1,
            block=int(timeout * 1000) if timeout > 0 else None,
        )

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
                if await self._deliver_to_consumer(queue_name, message):
                    return True
                # Fanout has nothing to restore to: the stream offset has moved
                # and there is no per-message key. Drop the tag and report the
                # read as undelivered.
                self._fanout_tags.discard(delivery_tag)
                logger.debug("No consumer for fanout queue %s; dropping delivery", queue_name)
                return False

        return False

    # ---- message creation / delivery ---------------------------------------

    def _create_message(
        self,
        queue: str,
        payload: dict,
        delivery_tag: str,
        delivery_count: int = 0,
    ) -> Message:
        """Create a Message from decoded payload dict.

        The AMQP redelivery flags are derived from ``delivery_count`` rather than
        a stored field of their own, so every path that can redeliver moves one
        counter. ``delivery_info['redelivered']`` is where kombu's own redis
        transport puts the flag and the only place celery looks for it when
        applying ``worker_deduplicate_successful_tasks``.
        """
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

        if delivery_count > 0:
            headers["x-delivery-count"] = delivery_count

        return Message(
            body=body,
            delivery_tag=delivery_tag,
            content_type=content_type,
            content_encoding=content_encoding,
            delivery_info={
                "exchange": "",
                "routing_key": queue,
                "redelivered": delivery_count > 0,
            },
            properties=properties,
            headers=headers,
            channel=self,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )

    async def _deliver_to_consumer(
        self,
        queue: str,
        message: Message,
    ) -> bool:
        """Find matching consumer and deliver message.

        Returns False when the queue has no consumer any more. A consume
        iteration captures its queue list when it starts and then blocks in
        Redis for up to ``block_timeout``, so a ``basic_cancel`` landing in
        that window pops a message nobody is listening for. Callers put it
        back rather than leaving it invisible until the visibility timeout,
        which would also count a redelivery against ``delivery_limit``.
        """
        # Every message this transport builds carries a delivery tag.
        delivery_tag: str = message.delivery_tag  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
        for q, callback, no_ack in self._consumers.values():
            if q == queue:
                if not no_ack:
                    self._delivered[delivery_tag] = (queue, message)

                try:
                    body = message.decode()
                except Exception:
                    # The consumer gets the raw body and decides what to do
                    # with it; the transport cannot know which content types
                    # this consumer accepts.
                    logger.warning(
                        "Could not decode message %s on queue %r; passing the raw body.",
                        delivery_tag,
                        queue,
                        exc_info=True,
                    )
                    body = message.body

                try:
                    result = callback(body, message)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as error:
                    await self._on_callback_failure(queue, delivery_tag, error)
                return True
        return False

    async def _on_callback_failure(
        self,
        queue: str,
        delivery_tag: str,
        error: Exception,
    ) -> None:
        """Requeue a message whose consumer callback raised.

        The attempt counts as a delivery, so the message goes back through the
        requeue script rather than a plain ZADD: it increments
        ``delivery_count``, which is what makes ``delivery_limit`` drop a
        payload that breaks the callback every single time. Without the
        increment such a message circulates forever.

        It goes back at its own priority score rather than at the head, so the
        rest of the queue is served before the next attempt.
        """
        logger.error(
            "Consumer callback for queue %r raised on message %s; requeuing it.",
            queue,
            delivery_tag,
            exc_info=error,
        )
        self._delivered.pop(delivery_tag, None)
        if delivery_tag in self._fanout_tags:
            # A fanout delivery has no message hash to requeue from and the
            # stream offset has already moved past it.
            self._fanout_tags.discard(delivery_tag)
            return
        await self._requeue_by_tag(delivery_tag)

    # ---- ack / reject / recover -------------------------------------------

    async def basic_ack(self, delivery_tag: str, multiple: bool = False) -> None:
        if delivery_tag in self._fanout_tags:
            self._fanout_tags.discard(delivery_tag)
            self._delivered.pop(delivery_tag, None)
            return

        entry = self._delivered.pop(delivery_tag, None)
        if entry:
            queue, _ = entry
            # Atomic ack via Lua script (ZREM + ZREM + DEL in one round-trip)
            script = await self._get_ack_script()
            await script(
                keys=[
                    self._messages_index_key(queue),
                    self._message_key(delivery_tag),
                    self._queue_key(queue),
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
                        self._queue_key(queue),
                    ],
                    args=[delivery_tag],
                )

    async def _restore_prefetch_buffer(self, queue: str | None = None) -> None:
        """Put back messages claimed for this channel but never handed out."""
        keep: deque[tuple[str, str, str, int]] = deque()
        while self._prefetch_buffer:
            claimed = self._prefetch_buffer.popleft()
            if queue is not None and claimed[0] != queue:
                keep.append(claimed)
                continue
            await self._restore_to_queue(claimed[0], claimed[1])
        self._prefetch_buffer = keep

    async def basic_recover(self, requeue: bool = True) -> None:
        await self._restore_prefetch_buffer()
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
        the messages_index with a new queue_at score. It also enforces
        ``delivery_limit``, since a consumer rejecting in a tight loop never
        lets the sweep see the message come due.

        Returns False when the message was not found or was dropped at the
        delivery limit.
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
                -1 if self._delivery_limit is None else self._delivery_limit,
            ],
        )
        if result == -1:
            logger.warning(
                "Dropped message %s: requeue would exceed delivery_limit=%s.",
                delivery_tag,
                self._delivery_limit,
            )
            return False
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
                await asyncio.sleep(self._requeue_check_interval)
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

    async def _enqueue_due_messages(self) -> SweepStats:
        """Run Lua script to enqueue messages whose queue_at has passed."""
        # Same predicate the consume path uses, and deliberately the same
        # order-preserving dedupe: a set here made the sweep visit queues in
        # hash order, so which backlog got restored first under the batch limit
        # differed between two workers with identical configuration.
        active_queues = self._regular_queues()
        if not active_queues:
            return SweepStats()

        now = time()
        threshold = now + self._requeue_check_interval
        totals = SweepStats()
        script = await self._get_enqueue_script()

        # Compute delivery_limit arg (-1 = no limit)
        limit = -1 if self._delivery_limit is None else self._delivery_limit

        for queue in active_queues:
            try:
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
                        limit,
                        DROPPED_REPORT_LIMIT,
                    ],
                )
                if not result:
                    continue
                enqueued, dropped, redelivered, orphaned, dropped_payloads = result
                if dropped:
                    # The script deleted these hashes, so this line is the only
                    # remaining trace of the messages.
                    described = ", ".join(self._describe_message(payload) for payload in dropped_payloads)
                    if dropped > len(dropped_payloads):
                        described += ", ..."
                    logger.error(
                        "Queue %s: %d message(s) dropped after reaching the delivery limit of %s: %s",
                        queue,
                        dropped,
                        self._delivery_limit,
                        described,
                    )
                if redelivered:
                    logger.info(
                        "Queue %s: %d message(s) redelivered after their visibility timeout expired.",
                        queue,
                        redelivered,
                    )
                if orphaned:
                    logger.info(
                        "Queue %s: removed %d orphaned index entries (message already acked or expired).",
                        queue,
                        orphaned,
                    )
                if enqueued >= DEFAULT_REQUEUE_BATCH_LIMIT:
                    logger.warning(
                        "Queue %s hit the enqueue batch limit of %d. There may be more messages waiting.",
                        queue,
                        DEFAULT_REQUEUE_BATCH_LIMIT,
                    )
                totals = SweepStats(
                    totals.enqueued + int(enqueued),
                    totals.dropped + int(dropped),
                    totals.redelivered + int(redelivered),
                    totals.orphaned + int(orphaned),
                )
            except Exception:
                # One unreachable or misbehaving queue must not cost the others
                # their sweep; the next cycle retries it in 60s anyway.
                logger.warning(
                    "Failed to enqueue due messages for queue %s, will retry next cycle",
                    queue,
                    exc_info=True,
                )

        return totals

    @staticmethod
    def _describe_message(payload: bytes | str) -> str:
        """Name a payload for a log line, its hash being already deleted."""
        try:
            message = json_loads(
                payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload,
            )
            headers = message.get("headers") or {}
            task = headers.get("task")
            task_id = headers.get("id")
            if task or task_id:
                return f"{task or '<unknown task>'} (id {task_id or '?'})"
            delivery_tag = (message.get("properties") or {}).get("delivery_tag")
            if delivery_tag:
                return f"<non-task message {delivery_tag}>"
        except Exception:
            logger.debug("Could not decode a dropped message payload for logging.", exc_info=True)
        return "<undecodable message>"

    async def _update_messages_index(self) -> None:
        """Update scores of delivered messages to prevent premature requeue."""
        if not self._delivered:
            return
        queue_at = time() + self._visibility_timeout + self._requeue_check_interval
        async with self.client.pipeline(transaction=False) as pipe:
            for tag, (queue, _) in list(self._delivered.items()):
                if tag not in self._fanout_tags:
                    index_key = self._messages_index_key(queue)
                    # XX = only update if member already exists
                    await pipe.zadd(index_key, {tag: queue_at}, xx=True)
            await pipe.execute()

    async def _refresh_queue_expires(self) -> None:
        """Refresh the queue, index and binding keys of queues with x-expires.

        A binding lives exactly as long as some channel keeps rescoring it. The
        rescore also re-adds a member another process pruned while this one was
        stalled, so a queue that is still declared here keeps its route.
        """
        if not self._expires:
            return
        now = time()
        touch: set[tuple[str, str]] = set()
        async with self.client.pipeline(transaction=False) as pipe:
            for queue, ttl_ms in self._expires.items():
                await pipe.pexpire(self._queue_key(queue), ttl_ms)
                await pipe.pexpire(self._messages_index_key(queue), ttl_ms)
                # GT, as in _put_message: never pull a deadline backwards that
                # another channel pushed further out.
                stale_at = self._binding_stale_at(queue, now=now)
                for exchange, member in self._binding_members.get(queue, ()):
                    await pipe.zadd(self._binding_key(exchange), {member: stale_at}, gt=True)
                    touch.add((exchange, queue))
            await pipe.execute()
        # Off the pipeline because the bootstrap needs the PTTL reply:
        # PEXPIRE GT declines on a key that lost its TTL.
        for exchange, queue in touch:
            await self._touch_binding_key(exchange, queue)

    # ---- get() and close() ------------------------------------------------

    async def get(
        self,
        queue: str,
        no_ack: bool = False,
        accept: AbstractSet[str] | None = None,
    ) -> Message | None:
        """Fetch one message without blocking, or None when the queue is empty.

        A broker failure is not an empty queue: the script call is left to
        raise, so a caller such as ``SimpleQueue.get_nowait`` reports the
        outage instead of a spurious ``Empty``.
        """
        queue_key = self._queue_key(queue)
        new_queue_at = time() + self._visibility_timeout + self._requeue_check_interval

        script = await self._get_consume_script()
        result = await script(
            keys=[queue_key],
            args=[
                self._global_keyprefix,
                MESSAGE_KEY_PREFIX,
                str(new_queue_at),
                MESSAGES_INDEX_PREFIX,
                "1",
                queue,
                "1" if no_ack else "0",
            ],
        )

        if not result:
            return None

        queue_name, delivery_tag, payload_json, delivery_count = self._parse_consume_result(result)
        payload = json_loads(payload_json)
        message = self._create_message(queue_name, payload, delivery_tag, delivery_count)
        if not no_ack:
            self._delivered[delivery_tag] = (queue_name, message)
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        # Deregister first: celery opens a channel per unit of work on some
        # paths, and the transport would otherwise hold every one of them
        # until it is closed itself. Doing it here rather than at the end also
        # covers a close that is cancelled while draining.
        self._transport.forget_channel(self)

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
                    task.cancel()
                await asyncio.gather(*consumer_tasks, return_exceptions=True)
        self._consume_iter_task = None
        self._xread_iter_task = None

        # Cancel periodic tasks
        periodic_tasks = [t for t in (self._enqueue_task, self._heartbeat_task, self._expires_task) if t is not None]
        for periodic_task in periodic_tasks:
            periodic_task.cancel()
        await asyncio.gather(*periodic_tasks, return_exceptions=True)

        await self._restore_prefetch_buffer()

        # Requeue unacked messages. The snapshot is iterated across awaits and
        # basic_ack runs on the same loop, so a tag can be acked mid-drain;
        # skipping it then keeps the intent explicit rather than relying on the
        # requeue script's missing-hash guard to no-op.
        for delivery_tag, (queue, _) in list(self._delivered.items()):
            if delivery_tag not in self._fanout_tags and delivery_tag in self._delivered:
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

        for queue in list(self.auto_delete_queues):
            try:
                await self.queue_delete(queue)
            except Exception:
                # The rest of the close still has to happen, so this is
                # reported rather than raised. The queue keeps its own expiry.
                logger.warning("Could not delete auto-delete queue %r.", queue, exc_info=True)

        self._consumers.clear()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


_Channel = Channel


class Transport(BaseTransport):
    """Pure asyncio Valkey/Redis transport with priority queues, reliable fanout, and delayed delivery.

    Uses two clients:
    - Main client for BZMPOP, sorted set ops, hash ops, publish
    - Sub-client dedicated to XREAD BLOCK for fanout streams

    Supports both valkey-py and redis-py. The URL scheme selects which library
    to prefer (with fallback if only one is installed).
    """

    Channel = _Channel  # type: ignore[assignment]
    default_port = 6379

    driver_type = "redis"
    driver_name = "redis"

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
        self.driver_name = self._lib.__name__

        super().__init__(url, **options)
        self._url = normalize_url(self._url, self._lib)
        self._client = None
        self._subclient = None
        self._channels: list[Channel] = []
        self._connected = False
        self._db = _parse_db_from_url(url)
        #: Serialises connect against close and against a second connect, so
        #: two callers racing for the first channel cannot each build a pair
        #: of clients and leave one pair connected with nothing referencing it.
        self._lock = asyncio.Lock()

    #: Options this transport consumes itself. Anything else in
    #: ``transport_options`` is a client keyword argument and is forwarded to
    #: ``from_url``, which rejects names it does not know. Every option the
    #: module docstring lists has to appear either here or in the client's
    #: signature; ``tests/kombu/unit/transport/test_valkey_redis.py`` checks it.
    _TRANSPORT_ONLY_OPTIONS = frozenset(
        {
            "block_timeout",
            "credential_provider",
            "delivery_limit",
            "fanout_prefix",
            "global_keyprefix",
            "message_ttl",
            "queue_expires",
            "requeue_check_interval",
            "stream_maxlen",
            "visibility_timeout",
        },
    )

    def _client_kwargs(self) -> dict[str, Any]:
        """Client keyword arguments, with a socket timeout that fits the block.

        redis-py and valkey-py read a reply under their own socket timeout,
        five seconds by default, and pass no separate deadline for a blocking
        command. A BZMPOP or XREAD blocking longer than that fails with a read
        timeout and the connection is dropped, which with the default
        ``block_timeout`` of ten seconds is every blocking consume on an idle
        queue. So the socket timeout is derived from the block unless the
        caller set one, and a setting that is too short is refused rather than
        breaking the consume loop.
        """
        kwargs = {k: v for k, v in self._options.items() if k not in self._TRANSPORT_ONLY_OPTIONS}
        block_timeout = _duration_option(self._options, "block_timeout", DEFAULT_BLOCK_TIMEOUT)
        configured = self._configured_socket_timeout()
        if configured is None:
            kwargs["socket_timeout"] = block_timeout + SOCKET_TIMEOUT_HEADROOM
        elif configured <= block_timeout:
            raise ValueError(
                f"socket_timeout ({configured}s) must be greater than block_timeout "
                f"({block_timeout}s), or every blocking consume ends in a read "
                "timeout and a dropped connection",
            )
        return kwargs

    def _configured_socket_timeout(self) -> float | None:
        """The socket timeout the caller asked for, if any.

        A query parameter on the URL wins over a keyword argument in
        ``from_url``, so both places have to be read.
        """
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self._url).query)
        values = query.get("socket_timeout")
        if values:
            return float(values[-1])
        if "socket_timeout" in self._options:
            return float(self._options["socket_timeout"])
        return None

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
            CredentialProvider = None

        if CredentialProvider is not None and not isinstance(credential_provider, CredentialProvider):
            raise ValueError(
                "credential_provider must be an instance of "
                f"{CredentialProvider.__module__}.CredentialProvider (or a subclass)",
            )

        return {"credential_provider": credential_provider}

    async def connect(self) -> None:
        async with self._lock:
            if self._connected:
                return

            client_kw = self._client_kwargs()
            client_kw.update(self._process_credential_provider())

            # Built into locals and published only once both answer. Each one
            # owns a connection pool that nothing else can reach until then, so
            # a failure has to close them here or the sockets stay open for the
            # life of the process.
            clients: list[Any] = []
            try:
                for _ in range(2):
                    clients.append(
                        self._aiolib.from_url(self._url, decode_responses=False, **client_kw),
                    )
                    await clients[-1].ping()
            except BaseException:
                await self._aclose_clients(clients)
                raise

            self._client, self._subclient = clients
            self._connected = True
            logger.debug("Connected via %s at %s (dual clients)", self._lib.__name__, self._url)

    async def close(self) -> None:
        async with self._lock:
            channels, self._channels = self._channels, []
            clients = [c for c in (self._subclient, self._client) if c is not None]
            self._client = self._subclient = None
            self._connected = False
            try:
                for channel in channels:
                    await channel.close()
            finally:
                # Draining a channel can be cancelled or can fail on a broker
                # that has gone away. Either way the sockets go.
                await self._aclose_clients(clients)

    @staticmethod
    async def _aclose_clients(clients: list[Any]) -> None:
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                # Reported rather than raised: this runs while another error is
                # already on its way out, or while the rest of the shutdown
                # still has to happen.
                logger.warning("Could not close a Redis client.", exc_info=True)

    def forget_channel(self, channel: _Channel) -> None:
        """Drop a channel that has closed itself."""
        if channel in self._channels:
            self._channels.remove(channel)

    async def create_channel(self) -> _Channel:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        await self.connect()
        channel = Channel(self)
        async with self._lock:
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
