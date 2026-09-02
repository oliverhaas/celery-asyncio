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
        # The app's connection for the loop this runs on, rather than one of
        # our own: the app closes it, so leaving this block has nothing to
        # close, which off a loop it could only do by blocking on the shared
        # loop and from a task body could only raise.
        conn = self.app.async_connection
        with self.Dispatcher(conn, hostname, enabled, channel=None, buffer_while_offline=buffer_while_offline) as d:
            # A dispatcher publishes on the loop it captured when it was built.
            # Built off a loop it captured none, and dropped every event it was
            # handed; the connection above belongs to the loop named here.
            d._event_loop = current_loop() or default_loop_runner().loop
            yield d
