# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Worker Event Dispatcher Bootstep - async implementation."""

import asyncio

from celery import bootsteps
from celery.utils.log import get_logger

from .connection import Connection

__all__ = ("Events",)

logger = get_logger(__name__)


class Events(bootsteps.StartStopStep):
    """Service used for sending monitoring events."""

    requires = (Connection,)

    #: Seconds a worker shutting down gives its event publishes to finish.
    shutdown_publish_timeout = 5.0

    def __init__(self, c, task_events=True, without_heartbeat=False, without_gossip=False, **kwargs):
        self.groups = None if task_events else ["worker"]
        self.send_events = task_events or not without_gossip or not without_heartbeat
        self.enabled = self.send_events
        c.event_dispatcher = None
        super().__init__(c, **kwargs)

    async def start(self, c):
        # flush events sent while connection was down.
        prev = await self._close(c)
        conn = await c.connection_for_write()
        dis = c.event_dispatcher = c.app.events.Dispatcher(
            conn,
            hostname=c.hostname,
            enabled=self.send_events,
            groups=self.groups,
            # Task events go out together, as one `task.multi` message per
            # pass of the worker loop. Upstream buffers them whenever it runs
            # its hub; this worker has no hub, and dropping the condition along
            # with it had every task event go out as a publish of its own.
            buffer_group=["task"],
            on_send_buffered=c.on_send_event_buffered,
        )
        if prev:
            dis.extend_buffer(prev)
            dis.flush()

    def stop(self, c):
        pass

    async def _close(self, c):
        if c.event_dispatcher:
            dispatcher = c.event_dispatcher
            # remember changes from remote control commands:
            self.groups = dispatcher.groups

            # Awaited, not scheduled: a task nothing holds can be collected
            # before it closes anything, and connections piled up on reconnect.
            if dispatcher.connection:
                try:
                    await dispatcher.connection.close()
                except (OSError, *c.connection_errors, *c.channel_errors):
                    logger.warning("Failed to close the event dispatcher connection", exc_info=True)
            # disable(), not close(): close() only drops the producer, so the
            # on_disabled callbacks never fire and Heart keeps ticking (upstream 8b4b29c93).
            dispatcher.disable()
            c.event_dispatcher = None
            return dispatcher

    async def shutdown(self, c):
        dispatcher = c.event_dispatcher
        if dispatcher is not None:
            # Buffered task events wait for the next pass of the loop, and there is none.
            dispatcher.flush(errors=False)
            await self._wait_for_publishes(dispatcher)
        await self._close(c)

    async def _wait_for_publishes(self, dispatcher):
        publishes = [asyncio.wrap_future(publish) for publish in dispatcher.pending_publishes()]
        if not publishes:
            return
        _, unfinished = await asyncio.wait(publishes, timeout=self.shutdown_publish_timeout)
        for publish in unfinished:
            publish.cancel()
        if unfinished:
            logger.warning("Dropped %d event publishes that had not finished at shutdown", len(unfinished))
