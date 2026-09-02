# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Implementation for the app.events shortcuts."""

from contextlib import contextmanager

from kombu.utils.eventloop import current_loop, default_loop_runner
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
        # The app's connection, not one of our own, so that leaving this
        # block has nothing to close off a loop.
        conn = self.app.async_connection
        with self.Dispatcher(conn, hostname, enabled, channel=None, buffer_while_offline=buffer_while_offline) as d:
            # Built off a loop, a dispatcher captures none and drops its
            # events. This is the loop the connection above belongs to.
            d._event_loop = current_loop() or default_loop_runner().loop
            yield d
