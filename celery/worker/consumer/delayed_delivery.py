"""Native delayed delivery functionality for Celery workers.

This module provides the DelayedDelivery bootstep which handles setup and configuration
of native delayed delivery functionality when using quorum queues.

NOTE: Currently disabled in celery-asyncio - requires AMQP transport with quorum queues.
"""

from celery import bootsteps

from .tasks import Tasks

__all__ = ("DelayedDelivery",)


class DelayedDelivery(bootsteps.StartStopStep):
    """Bootstep that sets up native delayed delivery functionality.

    Currently disabled - requires AMQP transport with quorum queue support.
    """

    requires = (Tasks,)

    def include_if(self, c):
        # Disabled until AMQP transport is implemented
        return False
