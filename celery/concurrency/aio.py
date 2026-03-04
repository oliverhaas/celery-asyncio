"""Multi-loop asyncio + thread pool for celery-asyncio.

Architecture:
  - N "loop worker" threads, each running its own asyncio event loop
    with a Semaphore(P) limiting concurrent async tasks per loop.
  - M "sync worker" threads via ThreadPoolExecutor for sync tasks.
  - Main thread owns the broker connection and dispatches tasks.

With Python 3.14t free-threading, all threads run with true parallelism.

This is the default pool for celery-asyncio workers.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any

from celery import signals
from celery.utils.log import get_logger

from .base import BasePool, apply_target

__all__ = ("TaskPool",)

logger = get_logger("celery.pool")

# Sentinel returned by _run_tracer_with_timeouts when hard timeout fires
# (on_timeout already handled reporting, so _run_async_task should skip callback).
_HARD_TIMEOUT = object()


class ApplyResult:
    """Wrapper around a Future that provides a .get() method."""

    def __init__(self, future: Future | asyncio.Task | None) -> None:
        self.f = future

    def get(self, timeout: float | None = None) -> Any:
        if isinstance(self.f, Future):
            return self.f.result(timeout)
        return None

    def wait(self, timeout: float | None = None) -> None:
        if isinstance(self.f, Future):
            from concurrent.futures import wait

            wait([self.f], timeout)

    def cancel(self) -> None:
        """Cancel the underlying task/future."""
        if self.f is not None:
            self.f.cancel()

    def terminate(self, signal=None) -> None:
        """Terminate the underlying task/future (alias for cancel)."""
        self.cancel()


class LoopWorker:
    """A worker thread running its own asyncio event loop.

    Each LoopWorker runs an independent event loop on a daemon thread.
    A semaphore limits the number of concurrent async tasks to
    ``concurrency``.
    """

    def __init__(self, concurrency: int, app: Any, index: int) -> None:
        self._concurrency = concurrency
        self._app = app
        self._index = index
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._active_count = 0
        self._active_count_lock = Lock()
        self._ready = threading.Event()
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        """Start the loop worker thread and wait until the loop is running."""
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"celery-loop-worker-{self._index}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        """Thread target: create and run an event loop forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._app.set_current()
        self._ready.set()
        self._loop.run_forever()

    def submit(self, coro_factory: Callable, *args: Any) -> None:
        """Submit a coroutine to this loop worker (thread-safe).

        The coroutine will be wrapped with the semaphore to limit
        concurrent execution.
        """
        with self._active_count_lock:
            self._active_count += 1
        # We need to create the coroutine from inside the target loop.
        # call_soon_threadsafe schedules a regular callback, so we
        # use it to create_task the semaphore-wrapped coroutine.
        self._loop.call_soon_threadsafe(self._schedule_task, coro_factory, args)

    def _schedule_task(self, coro_factory: Callable, args: tuple) -> None:
        """Create and schedule the task on this loop (called from loop thread)."""
        task = self._loop.create_task(self._run_with_semaphore(coro_factory, args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_with_semaphore(self, coro_factory: Callable, args: tuple) -> None:
        """Acquire semaphore, run the coroutine, then release."""
        async with self._semaphore:
            try:
                await coro_factory(*args)
            finally:
                with self._active_count_lock:
                    self._active_count -= 1

    def cancel_all(self) -> None:
        """Cancel all running async tasks on this loop (must be called from loop thread)."""
        for task in list(self._tasks):
            task.cancel()

    def stop(self) -> None:
        """Cancel all tasks, stop the event loop, and join the thread."""
        if self._loop:
            self._loop.call_soon_threadsafe(self.cancel_all)
            # Give tasks a brief interval to process their CancelledError
            # and run cleanup (callbacks, result reporting) before stopping.
            self._loop.call_soon_threadsafe(
                self._loop.call_later, 0.5, self._loop.stop,
            )
        if self._thread:
            self._thread.join(timeout=6)


class TaskPool(BasePool):
    """Multi-loop asyncio + thread pool.

    - Async tasks: dispatched round-robin to N loop worker threads,
      each with its own event loop and Semaphore(P) concurrency limit.
    - Sync tasks: dispatched to a ThreadPoolExecutor with M workers.

    With Python 3.14t free-threading, all threads run truly parallel.
    """

    body_can_be_buffer = True
    signal_safe = False
    task_join_will_block = False

    def __init__(
        self, *args: Any, loop_workers: int = 1, loop_concurrency: int = 10, sync_workers: int = 1, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._loop_worker_count = loop_workers
        self._loop_concurrency = loop_concurrency
        self._sync_worker_count = sync_workers
        self._loop_workers: list[LoopWorker] = []
        self._executor: ThreadPoolExecutor | None = None
        self._active_futures: set[Future] = set()
        self._stuck_thread_count = 0
        self._stuck_lock = Lock()

    def on_start(self) -> None:
        # Start N loop worker threads
        for i in range(self._loop_worker_count):
            w = LoopWorker(self._loop_concurrency, self.app, i)
            w.start()
            self._loop_workers.append(w)
        # Start M sync worker threads
        self._executor = ThreadPoolExecutor(max_workers=self._sync_worker_count)
        logger.info(
            "Pool started: %d loop workers (concurrency=%d each), %d sync workers",
            self._loop_worker_count,
            self._loop_concurrency,
            self._sync_worker_count,
        )
        signals.worker_process_init.send(sender=None)

    def on_stop(self) -> None:
        for f in list(self._active_futures):
            f.cancel()
        self._active_futures.clear()
        for w in self._loop_workers:
            w.stop()
        self._loop_workers.clear()
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

    def restart(self) -> None:
        """Restart the pool: stop all workers and start fresh."""
        self.on_stop()
        self.on_start()

    def _is_async_task(self, args: tuple) -> bool:
        """Check if the task referenced by args[0] is an async coroutine."""
        if self.app and args:
            task_name = args[0]
            try:
                task = self.app.tasks[task_name]
                return asyncio.iscoroutinefunction(task.run)
            except (KeyError, AttributeError):
                pass
        return False

    def _pick_loop_worker(self) -> LoopWorker:
        """Pick the loop worker with the fewest active tasks."""
        return min(self._loop_workers, key=lambda w: w._active_count)

    def on_apply(
        self,
        target: Callable,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        callback: Callable | None = None,
        accept_callback: Callable | None = None,
        timeout_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        **options: Any,
    ) -> ApplyResult:
        args = args or ()
        kwargs = kwargs or {}

        if self._is_async_task(args) and self._loop_workers:
            worker = self._pick_loop_worker()
            worker.submit(
                self._run_async_task,
                target,
                args,
                kwargs,
                callback,
                accept_callback,
                timeout_callback,
                soft_timeout,
                timeout,
            )
            return ApplyResult(None)
        else:
            return self._apply_sync_task(
                target, args, kwargs, callback, accept_callback,
                timeout_callback=timeout_callback,
                soft_timeout=soft_timeout,
                timeout=timeout,
                **options,
            )

    async def _run_async_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        timeout_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
    ) -> None:
        """Execute an async task using the async tracer."""
        from kombu.serialization import loads as loads_message
        from kombu.serialization import prepare_accept_content

        from celery.app.trace import build_async_tracer

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
                    body,
                    content_type,
                    content_encoding,
                    accept=accept,
                )
            else:
                task_args, task_kwargs, embed = body

            request.update(
                {
                    "args": task_args,
                    "kwargs": task_kwargs,
                    "hostname": request.get("hostname", ""),
                    "is_eager": False,
                },
                **(embed or {}),
            )

            task_obj = app.tasks[task_name]

            tracer = build_async_tracer(
                task_name,
                task_obj,
                app=app,
            )

            tracer_result = await self._run_tracer_with_timeouts(
                tracer, uuid, task_args, task_kwargs, request,
                soft_timeout=soft_timeout,
                timeout=timeout,
                timeout_callback=timeout_callback,
            )

            # Hard timeout returns _HARD_TIMEOUT sentinel —
            # on_timeout already handled reporting.
            if tracer_result is _HARD_TIMEOUT:
                return

            R, I, T, Rstr = tracer_result

            result = (1, R, T) if I else (0, Rstr, T)
            if callback:
                callback(result)
        except asyncio.CancelledError:
            from celery.exceptions import ExceptionInfo, Terminated

            if callback:
                exc = Terminated("cancelled")
                callback((1, ExceptionInfo((type(exc), exc, None)), 0))
        except Exception:
            from celery.exceptions import ExceptionInfo

            if callback:
                callback((1, ExceptionInfo(), 0))

    async def _run_tracer_with_timeouts(
        self,
        tracer: Callable,
        uuid: str,
        task_args: tuple,
        task_kwargs: dict,
        request: dict,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        timeout_callback: Callable | None = None,
    ) -> tuple:
        """Run the async tracer with soft and hard timeout support.

        Soft timeout: cancels the asyncio Task; the CancelledError is caught
        and converted to SoftTimeLimitExceeded so it's reported correctly.
        The user's task code sees CancelledError at the await point (asyncio
        limitation), but if it propagates uncaught it becomes
        SoftTimeLimitExceeded for backend recording.

        Hard timeout: cancels the task and reports failure via timeout_callback.
        """
        from celery.exceptions import SoftTimeLimitExceeded

        _soft_timed_out = False
        _soft_handle = None

        async def _run_with_soft_timeout() -> tuple:
            nonlocal _soft_timed_out, _soft_handle

            if soft_timeout:
                current = asyncio.current_task()

                def _fire_soft_timeout():
                    nonlocal _soft_timed_out
                    _soft_timed_out = True
                    current.cancel()

                loop = asyncio.get_event_loop()
                _soft_handle = loop.call_later(soft_timeout, _fire_soft_timeout)

            try:
                return await tracer(uuid, task_args, task_kwargs, request)
            except asyncio.CancelledError:
                if _soft_timed_out:
                    raise SoftTimeLimitExceeded(soft_timeout) from None
                raise
            finally:
                if _soft_handle is not None:
                    _soft_handle.cancel()

        if timeout:
            try:
                return await asyncio.wait_for(
                    _run_with_soft_timeout(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                if timeout_callback:
                    timeout_callback(False, timeout)
                # Don't raise — on_timeout already handled task_ready + mark_as_failure.
                return _HARD_TIMEOUT
        else:
            return await _run_with_soft_timeout()

    def _apply_sync_task(
        self,
        target: Callable,
        args: tuple,
        kwargs: dict,
        callback: Callable | None,
        accept_callback: Callable | None,
        timeout_callback: Callable | None = None,
        soft_timeout: float | None = None,
        timeout: float | None = None,
        **options: Any,
    ) -> ApplyResult:
        """Run a sync task in the thread pool."""
        app = self.app
        f = self._executor.submit(
            self._run_in_thread,
            app,
            target,
            args,
            kwargs,
            callback,
            accept_callback,
        )
        self._active_futures.add(f)
        f.add_done_callback(self._active_futures.discard)

        if timeout:
            self._schedule_sync_timeout(f, timeout, timeout_callback)

        return ApplyResult(f)

    def _schedule_sync_timeout(
        self,
        future: Future,
        timeout: float,
        timeout_callback: Callable | None,
    ) -> None:
        """Schedule a hard timeout check for a sync task in the thread pool."""
        def _check_timeout():
            if not future.done():
                logger.error(
                    "Hard time limit (%ss) exceeded for sync task in thread pool. "
                    "Thread cannot be killed; will trigger process restart.",
                    timeout,
                )
                if timeout_callback:
                    timeout_callback(False, timeout)
                with self._stuck_lock:
                    self._stuck_thread_count += 1
        timer = threading.Timer(timeout, _check_timeout)
        timer.daemon = True
        timer.start()

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
        info.update(
            {
                "implementation": "asyncio+threads",
                "loop-workers": self._loop_worker_count,
                "loop-concurrency": self._loop_concurrency,
                "sync-workers": self._sync_worker_count,
                "loop-active": [w._active_count for w in self._loop_workers],
            }
        )
        return info
