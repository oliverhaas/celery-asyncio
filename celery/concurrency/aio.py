"""Hybrid asyncio + thread pool for celery-asyncio.

Async tasks run directly on the event loop (zero overhead).
Sync tasks run in a ThreadPoolExecutor (true parallelism with 3.14t free-threading).

This is the default pool for celery-asyncio workers.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

from celery import signals
from celery.utils.log import get_logger

from .base import BasePool, apply_target

__all__ = ('TaskPool',)

logger = get_logger('celery.pool')

if TYPE_CHECKING:
    pass


class ApplyResult:
    """Wrapper around a Future that provides a .get() method."""

    def __init__(self, future: Future | asyncio.Task) -> None:
        self.f = future

    def get(self, timeout: float | None = None) -> Any:
        if isinstance(self.f, Future):
            return self.f.result(timeout)
        # asyncio.Task - can't block on it from sync code
        return None

    def wait(self, timeout: float | None = None) -> None:
        if isinstance(self.f, Future):
            from concurrent.futures import wait
            wait([self.f], timeout)


class TaskPool(BasePool):
    """Hybrid asyncio + thread pool.

    - Async tasks: executed directly on the asyncio event loop
    - Sync tasks: dispatched to a ThreadPoolExecutor

    With Python 3.14t free-threading, sync tasks in threads run
    with true parallelism (no GIL).
    """

    body_can_be_buffer = True
    signal_safe = False
    task_join_will_block = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._executor: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def on_start(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=self.limit)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        signals.worker_process_init.send(sender=None)

    def on_stop(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

    def on_apply(
        self,
        target: Callable,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        callback: Callable | None = None,
        accept_callback: Callable | None = None,
        **options: Any,
    ) -> ApplyResult:
        args = args or ()
        kwargs = kwargs or {}

        # Check if this task is async.
        # args[0] is the task name (from trace_task_ret / fast_trace_task).
        is_async = False
        if self.app and args:
            task_name = args[0]
            try:
                task = self.app.tasks[task_name]
                import asyncio as _aio
                is_async = _aio.iscoroutinefunction(task.run)
            except (KeyError, AttributeError):
                pass

        if is_async and self._loop and self._loop.is_running():
            return self._apply_async_task(
                target, args, kwargs, callback, accept_callback, **options
            )
        else:
            return self._apply_sync_task(
                target, args, kwargs, callback, accept_callback, **options
            )

    def _apply_async_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        **options: Any,
    ) -> ApplyResult:
        """Run an async task directly on the event loop."""
        task = self._loop.create_task(
            self._run_async_task(target, args, kwargs, callback, accept_callback)
        )
        return ApplyResult(task)

    async def _run_async_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
    ) -> None:
        """Execute an async task using the async tracer."""
        import time
        from celery.app.trace import (
            build_async_tracer,
            trace_task,
        )
        from kombu.serialization import loads as loads_message, prepare_accept_content

        # Unpack args: (task_name, uuid, request, body, content_type, content_encoding)
        task_name, uuid, request, body, content_type, content_encoding = args[:6]

        if accept_callback:
            accept_callback(os.getpid(), time.monotonic())

        try:
            app = self.app
            embed = None
            if content_type:
                accept = prepare_accept_content(app.conf.accept_content)
                task_args, task_kwargs, embed = loads_message(
                    body, content_type, content_encoding, accept=accept,
                )
            else:
                task_args, task_kwargs, embed = body

            request.update({
                'args': task_args, 'kwargs': task_kwargs,
                'hostname': request.get('hostname', ''),
                'is_eager': False,
            }, **(embed or {}))

            task_obj = app.tasks[task_name]

            # Build the async tracer and execute
            tracer = build_async_tracer(
                task_name, task_obj, app=app,
            )
            R, I, T, Rstr = await tracer(uuid, task_args, task_kwargs, request)

            result = (1, R, T) if I else (0, Rstr, T)
            if callback:
                callback(result)
        except Exception:
            from celery.exceptions import ExceptionInfo
            if callback:
                callback(ExceptionInfo())

    def _apply_sync_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        **options: Any,
    ) -> ApplyResult:
        """Run a sync task in the thread pool.

        Sets the celery app context in the thread so trace_task_ret
        can find the app and its registered tasks.
        """
        app = self.app
        f = self._executor.submit(
            self._run_in_thread, app, target, args, kwargs,
            callback, accept_callback,
        )
        return ApplyResult(f)

    @staticmethod
    def _run_in_thread(
        app: Any,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
    ) -> Any:
        """Execute apply_target with the correct app context."""
        app.set_current()
        return apply_target(target, args, kwargs, callback, accept_callback)

    def _get_info(self) -> dict[str, Any]:
        info = super()._get_info()
        info.update({
            'max-concurrency': self.limit,
            'threads': len(self._executor._threads) if self._executor else 0,
            'implementation': 'asyncio+threads',
        })
        return info
