# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Consumer Broker Connection Bootstep - async implementation."""

from celery import bootsteps
from celery.utils.log import get_logger

__all__ = ("Connection",)

logger = get_logger(__name__)
info = logger.info


class Connection(bootsteps.StartStopStep):
    """Service managing the consumer broker connection."""

    def __init__(self, c, **kwargs):
        c.connection = None
        super().__init__(c, **kwargs)

    async def start(self, c):
        c.connection = await c.connect()
        info("Connected to %s", c.connection.as_uri())

    async def shutdown(self, c):
        connection, c.connection = c.connection, None
        if connection:
            try:
                await connection.close()
            except Exception:
                pass

    def info(self, c):
        params = "N/A"
        if c.connection:
            params = c.connection.info()
            params.pop("password", None)
        return {"broker": params}
