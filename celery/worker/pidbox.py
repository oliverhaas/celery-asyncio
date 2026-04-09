"""Worker Pidbox (remote control) - async implementation."""

import asyncio

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

    async def on_message(self, body, message):
        """Handle incoming control messages (async).

        This is an async callback - kombu's Consumer detects the returned
        coroutine and schedules it as a task.
        """
        self._forward_clock()
        try:
            result = self.node.handle_message(body, message)
            if asyncio.iscoroutine(result):
                await result
        except KeyError as exc:
            error("No such control command: %s", exc)
        except Exception as exc:
            error("Control command error: %r", exc, exc_info=True)
            await self._async_reset()

    async def start(self, c):
        # Use the default channel so pidbox messages are delivered
        # by the same drain_events loop as task messages.
        self.node.channel = c.connection.default_channel
        self.consumer = await self.node.listen(callback=self.on_message)
        self.consumer.on_decode_error = c.on_decode_error

    async def stop(self, c):
        # Don't close the default channel - it's shared.
        if self.consumer:
            try:
                await self.consumer.cancel()
            except Exception:
                pass

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
