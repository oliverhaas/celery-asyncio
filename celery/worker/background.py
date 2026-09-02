"""Coroutines the worker starts from synchronous code without awaiting them."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from celery.utils.log import get_logger

__all__ = ("spawn", "spawn_threadsafe")

logger = get_logger(__name__)

#: The loop holds running tasks only weakly, so every task started here
#: stays in this set until it is done.
_running: set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("Background task %s failed: %r", task.get_name(), exc, exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
    """Start `coro` on the running loop and return its task without awaiting it.

    The caller must be on the event loop thread; `spawn_threadsafe` is for the
    ones that are not.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finished)
    return task


def spawn_threadsafe(coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop, name: str | None = None) -> None:
    """Start `coro` on `loop` from any thread."""
    loop.call_soon_threadsafe(spawn, coro, name)
