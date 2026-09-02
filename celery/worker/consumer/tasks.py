# Partially from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Worker Task Consumer Bootstep - async implementation."""

from kombu.common import QoS

from celery import bootsteps
from celery.utils.log import get_logger

from .mingle import Mingle

__all__ = ("Tasks",)


logger = get_logger(__name__)
debug = logger.debug


class Tasks(bootsteps.StartStopStep):
    """Bootstep starting the task message consumer."""

    requires = (Mingle,)

    def __init__(self, c, **kwargs):
        c.task_consumer = c.qos = None
        super().__init__(c, **kwargs)

    async def start(self, c):
        """Start task consumer."""
        c.update_strategies()

        # Create task consumer
        c.task_consumer = c.app.amqp.TaskConsumer(
            c.connection,
            on_decode_error=c.on_decode_error,
        )

        # On Redis the count is a fetch batch size, not a cap on unacked
        # messages; see the transport's basic_qos.
        c.qos = QoS(
            c.task_consumer.qos,
            c.initial_prefetch_count,
        )
        await c.qos.update()

    async def stop(self, c):
        """Stop task consumer."""
        if c.task_consumer:
            debug("Canceling task consumer...")
            try:
                await c.task_consumer.cancel()
            except (OSError, *c.connection_errors, *c.channel_errors):
                # A broker that is already gone cannot be told to stop
                # sending, which is what stopping the consumer is for.
                logger.debug("Error cancelling task consumer", exc_info=True)

    async def shutdown(self, c):
        """Shutdown task consumer."""
        if c.task_consumer:
            await self.stop(c)
            debug("Closing consumer channel...")
            try:
                await c.task_consumer.close()
            except (OSError, *c.connection_errors, *c.channel_errors):
                logger.debug("Error closing task consumer", exc_info=True)
            c.task_consumer = None

    def info(self, c):
        """Return task consumer info."""
        return {"prefetch_count": c.qos.value if c.qos else "N/A"}
