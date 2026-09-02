# Originally from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Consumer Broker Connection Bootstep - async implementation."""

from kombu.utils.url import maybe_sanitize_url

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
            except (OSError, *c.connection_errors, *c.channel_errors):
                # The worker is on its way out and the socket is already
                # broken; releasing it is all that was left to do.
                logger.warning("Failed to close the broker connection", exc_info=True)

    def info(self, c):
        params = "N/A"
        if c.connection:
            params = c.connection.info()
            params.pop("password", None)  # don't send password.
            # `alternates` are whole failover URLs, so the password popped
            # above is still in there in userinfo form (upstream 56d80409a).
            alternates = params.get("alternates")
            if isinstance(alternates, str):
                params["alternates"] = maybe_sanitize_url(alternates)
            elif isinstance(alternates, (list, tuple)):
                params["alternates"] = [maybe_sanitize_url(url) if isinstance(url, str) else url for url in alternates]
        return {"broker": params}
