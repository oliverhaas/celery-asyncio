# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Worker Event Dispatcher Bootstep - async implementation."""

import asyncio

from celery import bootsteps

from .connection import Connection

__all__ = ("Events",)


class Events(bootsteps.StartStopStep):
    """Service used for sending monitoring events."""

    requires = (Connection,)

    def __init__(self, c, task_events=True, without_heartbeat=False, without_gossip=False, **kwargs):
        self.groups = None if task_events else ["worker"]
        self.send_events = task_events or not without_gossip or not without_heartbeat
        self.enabled = self.send_events
        c.event_dispatcher = None
        super().__init__(c, **kwargs)

    async def start(self, c):
        # flush events sent while connection was down.
        prev = self._close(c)
        conn = await c.connection_for_write()
        dis = c.event_dispatcher = c.app.events.Dispatcher(
            conn,
            hostname=c.hostname,
            enabled=self.send_events,
            groups=self.groups,
        )
        if prev:
            dis.extend_buffer(prev)
            dis.flush()

    def stop(self, c):
        pass

    def _close(self, c):
        if c.event_dispatcher:
            dispatcher = c.event_dispatcher
            # remember changes from remote control commands:
            self.groups = dispatcher.groups

            # close custom connection (async in kombu-asyncio)
            if dispatcher.connection:
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(dispatcher.connection.close())
                except Exception:
                    pass
            try:
                dispatcher.close()
            except Exception:
                pass
            c.event_dispatcher = None
            return dispatcher

    def shutdown(self, c):
        self._close(c)
