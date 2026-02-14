"""Worker Pidbox (remote control) - async implementation."""
from kombu.utils.encoding import safe_str

from celery.utils.collections import AttributeDict
from celery.utils.functional import pass1
from celery.utils.log import get_logger

from . import control

__all__ = ("Pidbox",)

logger = get_logger(__name__)
debug, error, info = logger.debug, logger.error, logger.info


class Pidbox:
    """Worker mailbox - async implementation."""

    consumer = None

    def __init__(self, c):
        self.c = c
        self.hostname = c.hostname
        self.node = c.app.control.mailbox.Node(
            safe_str(c.hostname),
            handlers=control.Panel.data,
            state=AttributeDict(
                app=c.app,
                hostname=c.hostname,
                consumer=c,
                tset=pass1,
            ),
        )
        self._forward_clock = self.c.app.clock.forward

    def on_message(self, body, message):
        # just increase clock as clients usually don't
        # have a valid clock to adjust with.
        self._forward_clock()
        try:
            self.node.handle_message(body, message)
        except KeyError as exc:
            error("No such control command: %s", exc)
        except Exception as exc:
            error("Control command error: %r", exc, exc_info=True)
            self.reset()

    async def start(self, c):
        self.node.channel = await c.connection.channel()
        self.consumer = self.node.listen(callback=self.on_message)
        self.consumer.on_decode_error = c.on_decode_error

    async def stop(self, c):
        if self.node and self.node.channel:
            try:
                await self.node.channel.close()
            except Exception:
                pass

    def reset(self):
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_reset())

    async def _async_reset(self):
        await self.stop(self.c)
        await self.start(self.c)

    async def shutdown(self, c):
        if self.consumer:
            debug("Canceling broadcast consumer...")
            try:
                await self.consumer.cancel()
            except Exception:
                pass
        await self.stop(c)
