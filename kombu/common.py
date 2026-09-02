# Partially from Kombu by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/kombu
"""Common Utilities - Pure asyncio implementation."""

import asyncio
import os
import threading
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_OID, uuid3, uuid4, uuid5

from .entity import Exchange, Queue
from .log import get_logger
from .utils.uuid import uuid

if TYPE_CHECKING:
    from .connection import Connection
    from .transport.base import Channel

__all__ = (
    "Broadcast",
    "QoS",
    "eventloop",
    "maybe_declare",
    "uuid",
)

#: Prefetch count can't exceed short.
PREFETCH_COUNT_MAX = 0xFFFF

logger = get_logger(__name__)

_node_id = None
_node_id_lock = threading.Lock()


def get_node_id():
    global _node_id
    if _node_id is None:
        with _node_id_lock:
            if _node_id is None:
                _node_id = uuid4().int
    return _node_id


def generate_oid(node_id, process_id, thread_id, instance):
    ent = f"{node_id:x}-{process_id:x}-{thread_id:x}-{id(instance):x}"
    try:
        ret = str(uuid3(NAMESPACE_OID, ent))
    except ValueError:
        ret = str(uuid5(NAMESPACE_OID, ent))
    return ret


def oid_from(instance, threads=True):
    return generate_oid(
        get_node_id(),
        os.getpid(),
        threading.current_thread().ident if threads else 0,
        instance,
    )


class Broadcast(Queue):
    """Broadcast queue.

    Convenience class used to define broadcast queues.

    Every queue instance will have a unique name,
    and both the queue and exchange is configured with auto deletion.

    Arguments:
        name: This is used as the name of the exchange.
        queue: By default a unique id is used for the queue
            name for every consumer.  You can specify a custom
            queue name here.
        unique: Always create a unique queue
            even if a queue name is supplied.
        **kwargs: See Queue for additional keyword arguments.
    """

    def __init__(
        self,
        name: str | None = None,
        queue: str | None = None,
        unique: bool = False,
        auto_delete: bool = True,
        exchange: Exchange | None = None,
        **kwargs: Any,
    ):
        if unique:
            queue = "{}.{}".format(queue or "bcast", uuid())
        else:
            queue = queue or f"bcast.{uuid()}"
        super().__init__(
            name=queue,
            auto_delete=auto_delete,
            exchange=(exchange if exchange is not None else Exchange(name or "", type="fanout")),
            **kwargs,
        )


async def maybe_declare(
    entity: Exchange | Queue,
    channel: Channel | None = None,
) -> bool:
    """Declare an exchange or a queue on a channel.

    Args:
        entity: Exchange or Queue to declare.
        channel: Channel to use for declaration.

    Returns:
        True.
    """
    if channel is None:
        raise ValueError("Channel is required for declaration")

    await entity.declare(channel)
    return True


async def eventloop(
    conn: Connection,
    limit: int | None = None,
    timeout: float | None = None,
    ignore_timeouts: bool = False,
):
    """Async generator for draining events from connection.

    Best practice async generator wrapper around Connection.drain_events.

    Able to drain events forever, with a limit, and optionally ignoring
    timeout errors (a timeout of 1 is often used in environments where
    the socket can get "stuck", and is a best practice for Kombu consumers).

    Example:
        async def run(conn):
            async for _ in eventloop(conn, timeout=1, ignore_timeouts=True):
                pass  # loop forever

        # With a limit:
        async for _ in eventloop(conn, limit=10, timeout=1):
            pass

    Args:
        conn: Connection instance.
        limit: Maximum number of iterations.
        timeout: Timeout for each drain_events call.
        ignore_timeouts: If True, continue on timeout instead of raising.

    Yields:
        None after each successful drain.
    """
    for _i in range(limit) if limit else count():
        try:
            await conn.drain_events(timeout=timeout)
            yield
        except TimeoutError:
            if timeout is not None and not ignore_timeouts:
                raise
            yield


class QoS:
    """Thread safe increment/decrement of a channels prefetch_count.

    Arguments:
        callback: Async function to set new prefetch count.
        initial_value: Initial prefetch count value.
        max_prefetch: Maximum allowed prefetch count. If specified,
            increment_eventually will not exceed this limit.
            If None (default), there is no upper limit.

    Example:
        >>> qos = QoS(channel.basic_qos, initial_prefetch_count=2)
        >>> await qos.update()  # set initial

        >>> qos.increment_eventually()
        >>> qos.decrement_eventually()

        >>> while True:
        ...     if qos.prev != qos.value:
        ...         await qos.update()
    """

    prev: int | None = None

    def __init__(
        self,
        callback,
        initial_value: int,
        max_prefetch: int | None = None,
    ):
        self.callback = callback
        self._mutex = threading.RLock()
        self.value = initial_value or 0
        self.max_prefetch = max_prefetch

    def increment_eventually(self, n: int = 1) -> int:
        """Increment the value, but do not update the channels QoS.

        Note:
            Call update() to apply changes. If max_prefetch is set,
            the value will not exceed this limit.
        """
        with self._mutex:
            if self.value:
                new_value = self.value + max(n, 0)
                if self.max_prefetch is not None and new_value > self.max_prefetch:
                    new_value = self.max_prefetch
                self.value = new_value
        return self.value

    def decrement_eventually(self, n: int = 1) -> int:
        """Decrement the value, but do not update the channels QoS.

        Note:
            Call update() to apply changes.
        """
        with self._mutex:
            if self.value:
                self.value -= n
                self.value = max(self.value, 1)
        return self.value

    async def set(self, pcount: int) -> int:
        """Set channel prefetch_count setting."""
        if pcount != self.prev:
            new_value = pcount
            if pcount > PREFETCH_COUNT_MAX:
                logger.warning("QoS: Disabled: prefetch_count exceeds %r", PREFETCH_COUNT_MAX)
                new_value = 0
            logger.debug("basic.qos: prefetch_count->%s", new_value)
            result = self.callback(prefetch_count=new_value)
            if asyncio.iscoroutine(result):
                await result
            self.prev = pcount
        return pcount

    async def update(self) -> int:
        """Update prefetch count with current value."""
        with self._mutex:
            value = self.value
        return await self.set(value)
