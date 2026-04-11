# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Implementation for the app.events shortcuts."""

import asyncio
from contextlib import contextmanager

from kombu.utils.objects import cached_property


class Events:
    """Implements app.events."""

    receiver_cls = "celery.events.receiver:EventReceiver"
    dispatcher_cls = "celery.events.dispatcher:EventDispatcher"
    state_cls = "celery.events.state:State"

    def __init__(self, app=None):
        self.app = app

    @cached_property
    def Receiver(self):
        return self.app.subclass_with_self(self.receiver_cls, reverse="events.Receiver")

    @cached_property
    def Dispatcher(self):
        return self.app.subclass_with_self(self.dispatcher_cls, reverse="events.Dispatcher")

    @cached_property
    def State(self):
        return self.app.subclass_with_self(self.state_cls, reverse="events.State")

    @contextmanager
    def default_dispatcher(self, hostname=None, enabled=True, buffer_while_offline=False):
        conn = self.app.connection_for_write()
        try:
            prod = self.app.amqp.Producer(conn)
            with self.Dispatcher(conn, hostname, enabled, channel=None, buffer_while_offline=buffer_while_offline) as d:
                d.producer = prod
                yield d
        finally:
            asyncio.run(conn.close())
