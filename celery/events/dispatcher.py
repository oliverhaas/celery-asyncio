# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Event dispatcher sends events."""

import asyncio
import os
import threading
import time
from collections import defaultdict, deque
from functools import partial

from kombu import Producer
from kombu.utils.eventloop import default_loop_runner

from celery.app import app_or_default
from celery.utils.log import get_logger
from celery.utils.nodenames import anon_nodename
from celery.utils.time import utcoffset

from .event import Event, get_exchange, group_from

__all__ = ("EventDispatcher",)

logger = get_logger(__name__)


class EventDispatcher:
    """Dispatches event messages.

    Arguments:
        connection (kombu.Connection): Connection to the broker.

        hostname (str): Hostname to identify ourselves as,
            by default uses the hostname returned by
            :func:`~celery.utils.anon_nodename`.

        groups (Sequence[str]): List of groups to send events for.
            :meth:`send` will ignore send requests to groups not in this list.
            If this is :const:`None`, all events will be sent.
            Example groups include ``"task"`` and ``"worker"``.

        enabled (bool): Set to :const:`False` to not actually publish any
            events, making :meth:`send` a no-op.

        channel (kombu.Channel): Can be used instead of `connection` to specify
            an exact channel to use when sending events.

        buffer_while_offline (bool): If enabled events will be buffered
            while the connection is down. :meth:`flush` must be called
            as soon as the connection is re-established.

    Note:
        You need to :meth:`close` this after use.
    """

    DISABLED_TRANSPORTS = {"sql"}

    app = None

    # set of callbacks to be called when :meth:`enabled`.
    on_enabled = None

    # set of callbacks to be called when :meth:`disabled`.
    on_disabled = None

    def __init__(
        self,
        connection=None,
        hostname=None,
        enabled=True,
        channel=None,
        buffer_while_offline=True,
        app=None,
        serializer=None,
        groups=None,
        delivery_mode=1,
        buffer_group=None,
        buffer_limit=24,
        on_send_buffered=None,
    ):
        self.app = app_or_default(app or self.app)
        self.connection = connection
        self.channel = channel
        self.hostname = hostname or anon_nodename()
        self.buffer_while_offline = buffer_while_offline
        self.buffer_group = buffer_group or frozenset()
        self.buffer_limit = buffer_limit
        self.on_send_buffered = on_send_buffered
        self._group_buffer = defaultdict(list)
        self.mutex = threading.Lock()
        self.producer = None
        # Bound the outbound buffer to prevent OOM when the broker is down.
        # At ~1KB per event, 10000 events ≈ 10MB worst case.
        self._outbound_buffer = deque(maxlen=10000)
        self.serializer = serializer or self.app.conf.event_serializer
        self.on_enabled = set()
        self.on_disabled = set()
        self.groups = set(groups or [])
        self.tzoffset = [-time.timezone, -time.altzone]
        self.clock = self.app.clock
        self.delivery_mode = delivery_mode
        if not connection and channel:
            self.connection = channel.connection.client
        self.enabled = enabled
        conninfo = self.connection or self.app.connection_for_write()
        self.exchange = get_exchange(conninfo, name=self.app.conf.event_exchange)
        driver_type = conninfo.transport.driver_type if conninfo.transport else getattr(conninfo, "_scheme", "")
        if driver_type in self.DISABLED_TRANSPORTS:
            self.enabled = False
        if self.enabled:
            self.enable()
        self.headers = {"hostname": self.hostname}
        self.pid = os.getpid()
        # Capture the event loop if we're created from an async context
        # (e.g. the Events bootstep). This allows _publish to schedule
        # coroutines from threads via call_soon_threadsafe.
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def enable(self):
        # The channel goes in the channel argument. Passing it first put it
        # where the connection belongs, and the producer then asked a channel
        # for a channel of its own the first time it published.
        self.producer = Producer(
            self.connection,
            channel=self.channel,
            exchange=self.exchange,
            serializer=self.serializer,
            auto_declare=False,
        )
        self.enabled = True
        for callback in self.on_enabled:
            callback()

    def disable(self):
        if self.enabled:
            self.enabled = False
            self.close()
            for callback in self.on_disabled:
                callback()

    def publish(self, type, fields, producer, blind=False, Event=Event, **kwargs):
        """Publish event using custom :class:`~kombu.Producer`.

        Arguments:
            type (str): Event type name, with group separated by dash (`-`).
                fields: Dictionary of event fields, must be json serializable.
            producer (kombu.Producer): Producer instance to use:
                only the ``publish`` method will be called.
            retry (bool): Retry in the event of connection failure.
            retry_policy (Mapping): Map of custom retry policy options.
                See :meth:`~kombu.Connection.ensure`.
            blind (bool): Don't set logical clock value (also don't forward
                the internal logical clock).
            Event (Callable): Event type used to create event.
                Defaults to :func:`Event`.
            utcoffset (Callable): Function returning the current
                utc offset in hours.
        """
        clock = None if blind else self.clock.forward()
        event = Event(type, hostname=self.hostname, utcoffset=utcoffset(), pid=self.pid, clock=clock, **fields)
        with self.mutex:
            return self._publish(event, producer, routing_key=type.replace("-", "."), **kwargs)

    def _publish(self, event, producer, routing_key, retry=False, retry_policy=None, utcoffset=utcoffset):
        if producer is None:
            # After a reconnect the old dispatcher is closed, but stale timers
            # such as Heart keep calling this. Without the guard every one of
            # them raised AttributeError and got buffered (upstream acce2acc7).
            return
        exchange = self.exchange
        try:
            coro = producer.publish(
                event,
                routing_key=routing_key,
                exchange=exchange.name,
                retry=retry,
                retry_policy=retry_policy,
                declare=[exchange],
                serializer=self.serializer,
                headers=self.headers,
                delivery_mode=self.delivery_mode,
            )
        except Exception as exc:
            self._publish_failed(event, routing_key, exc)
            return

        if not asyncio.iscoroutine(coro):
            return

        # `producer.publish()` is a coroutine in kombu, so nothing has left the
        # process yet and nothing can have failed yet either. A broker that is
        # down surfaces on the task, which is why the offline buffer has to be
        # filled from the done callback rather than from an except here.
        loop = self._event_loop
        if loop is None:
            # Built outside a running loop, by a client sending task-sent
            # events, say. The connection then belongs to the shared
            # background loop, which is the one that will close it as well.
            loop = default_loop_runner().loop
        elif loop.is_closed():
            coro.close()
            self._publish_failed(event, routing_key, RuntimeError("the loop this dispatcher publishes on is closed"))
            return

        done = partial(self._on_publish_done, event, routing_key)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(coro).add_done_callback(done)
        else:
            # On a LoopWorker loop, or on a thread with no loop at all.
            asyncio.run_coroutine_threadsafe(coro, loop).add_done_callback(done)

    def _on_publish_done(self, event, routing_key, outcome):
        """Buffer the event if the publish it was scheduled for failed."""
        if outcome.cancelled():
            return
        exc = outcome.exception()
        if exc is not None:
            self._publish_failed(event, routing_key, exc)

    def _publish_failed(self, event, routing_key, exc):
        if self.buffer_while_offline:
            # The entry holds no exception: that pins its traceback and every
            # frame below it, which for a dispatcher publishing every couple of
            # seconds against a dead broker is the leak (upstream 8b4b29c93).
            self._outbound_buffer.append((event, routing_key))
            logger.warning("Event %s buffered, publishing it failed: %r", routing_key, exc)
        else:
            logger.warning("Event %s dropped, publishing it failed: %r", routing_key, exc)

    def send(self, type, blind=False, utcoffset=utcoffset, retry=False, retry_policy=None, Event=Event, **fields):
        """Send event.

        Arguments:
            type (str): Event type name, with group separated by dash (`-`).
            retry (bool): Retry in the event of connection failure.
            retry_policy (Mapping): Map of custom retry policy options.
                See :meth:`~kombu.Connection.ensure`.
            blind (bool): Don't set logical clock value (also don't forward
                the internal logical clock).
            Event (Callable): Event type used to create event,
                defaults to :func:`Event`.
            utcoffset (Callable): unction returning the current utc offset
                in hours.
            **fields (Any): Event fields -- must be json serializable.
        """
        if self.enabled:
            groups, group = self.groups, group_from(type)
            if groups and group not in groups:
                return None
            if group in self.buffer_group:
                clock = self.clock.forward()
                event = Event(type, hostname=self.hostname, utcoffset=utcoffset(), pid=self.pid, clock=clock, **fields)
                buf = self._group_buffer[group]
                buf.append(event)
                if len(buf) >= self.buffer_limit:
                    self.flush()
                elif self.on_send_buffered:
                    self.on_send_buffered()
            else:
                return self.publish(
                    type, fields, self.producer, blind=blind, Event=Event, retry=retry, retry_policy=retry_policy
                )

    def flush(self, errors=True, groups=True):
        """Flush the outbound buffer."""
        if errors:
            buf = list(self._outbound_buffer)
            # Clear before republishing, not after: a failing _publish appends
            # the entry back, and clearing afterwards threw that away again
            # (upstream 10f24ce07).
            self._outbound_buffer.clear()
            with self.mutex:
                for event, routing_key in buf:
                    self._publish(event, self.producer, routing_key)
        if groups:
            with self.mutex:
                for group, events in self._group_buffer.items():
                    if not events:
                        continue
                    # Publish a detached copy. `producer.publish()` is a
                    # coroutine here, so the payload is not read until the task
                    # scheduled below actually runs, and handing over the live
                    # list meant the clear on the next line emptied the batch
                    # before anyone serialized it: every group-buffered flush
                    # went out as `[]`. Upstream (97ed017c0) had the milder
                    # version of this, where only the offline re-buffer was
                    # lost.
                    batch = list(events)
                    self._publish(batch, self.producer, "%s.multi" % group)
                    # Only what was published: events appended while the publish
                    # was in flight belong to the next flush (upstream
                    # f85031f61). Appends go to the tail and flushes are
                    # serialized by the mutex, so the leading slice is exact.
                    del events[: len(batch)]

    def extend_buffer(self, other):
        """Copy the outbound buffer of another instance."""
        self._outbound_buffer.extend(other._outbound_buffer)

    def close(self):
        """Close the event dispatcher."""
        try:
            self.mutex.release()
        except RuntimeError:
            pass
        self.producer = None
