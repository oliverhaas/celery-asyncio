"""One long-lived event loop, for reaching async code from synchronous callers."""

import asyncio
import atexit
import concurrent.futures
import contextvars
import os
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

__all__ = ("LoopRunner", "current_loop", "default_loop_runner")

_R = TypeVar("_R")


def current_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop running in this thread, or None if there is none."""

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel what is still running on `loop` and let it unwind.

    The same shutdown :func:`asyncio.run` performs, which this loop never gets
    because it is stopped rather than returned from. Without it a transport's
    long-lived background tasks (consumer iterations, heartbeats, expiry
    refreshes) are still pending when the loop closes, and the interpreter
    reports each of them as "Task was destroyed but it is pending!" on the way
    out, sometimes with a "no running event loop" traceback from whatever the
    dying coroutine tried to await next.
    """

    tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    for task in tasks:
        if not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during loop shutdown",
                    "exception": task.exception(),
                    "task": task,
                },
            )


class LoopRunner:
    """Runs coroutines on one event loop kept alive in a daemon thread."""

    # `asgiref.async_to_sync` builds a throwaway loop per call when the caller
    # is purely synchronous. Asyncio transports belong to the loop that opened
    # them, so nothing a coroutine reaches for can outlive a single call, and a
    # broker connection ends up opened and abandoned per published message.
    # One loop kept alive means what the first call opens, the second can use.

    def __init__(self, name: str = "celery-loop") -> None:
        self.name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pid: int | None = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The background loop, started on first use."""

        pid = os.getpid()
        with self._lock:
            loop = self._loop
            if loop is not None and self._pid == pid and not loop.is_closed():
                return loop
            # Either there is none yet, or we are looking at one inherited
            # through fork(): the thread running it did not come with us, so it
            # is not ours to stop and there is nothing left to drive it.
            self._loop = loop = asyncio.new_event_loop()
            self._pid = pid
            self._thread = threading.Thread(target=self._run, args=(loop,), name=self.name, daemon=True)
            self._thread.start()
            return loop

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                _cancel_all_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def run(self, coro: Coroutine[Any, Any, _R]) -> _R:
        """Run `coro` on the background loop, blocking until it returns.

        Refuses to run from inside a running loop, where the caller has an
        async form to await instead. A caller with no such choice, a sync
        dunder invoked by third-party code, uses `run_from_any_thread`.
        """

        if current_loop() is not None:
            coro.close()
            raise RuntimeError(
                "Cannot block on the background loop from inside a running event loop. "
                "Await the async form instead, for example asend_task() rather than send_task(), "
                "or adelay()/aapply_async() rather than delay()/apply_async()."
            )

        return self.run_from_any_thread(coro)

    def run_from_any_thread(self, coro: Coroutine[Any, Any, _R]) -> _R:
        """Run `coro` on the background loop, blocking the caller either way.

        The background loop runs in its own thread, so blocking a caller that
        has a loop of its own stalls that loop but cannot deadlock. For a sync
        API reached from async code there is nothing else to do, so this is
        what `Connection.__enter__` and friends use.

        The one caller it cannot serve is a coroutine already running on this
        runner's own loop: the loop would be waiting on work only it can run.
        """

        loop = self.loop
        if current_loop() is loop:
            coro.close()
            raise RuntimeError(
                "Cannot block on the background loop from a coroutine running on it. "
                "Await the async form instead, for example `async with Connection(...)` "
                "rather than `with Connection(...)`."
            )
        # Copied rather than inherited: the coroutine runs on another thread,
        # which has a context of its own that the caller never touched.
        context = contextvars.copy_context()
        outcome: concurrent.futures.Future[_R] = concurrent.futures.Future()

        def _finish(task: asyncio.Task) -> None:
            if task.cancelled():
                outcome.cancel()
                outcome.set_running_or_notify_cancel()
            elif (exc := task.exception()) is not None:
                outcome.set_exception(exc)
            else:
                outcome.set_result(task.result())

        def _start() -> None:
            try:
                task = loop.create_task(coro, context=context)
            except BaseException as exc:
                outcome.set_exception(exc)
            else:
                task.add_done_callback(_finish)

        loop.call_soon_threadsafe(_start)
        return outcome.result()

    def stop(self) -> None:
        """Stop the loop and its thread. A later run() starts a fresh one."""

        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = self._pid = None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)


_default_runner: LoopRunner | None = None
_default_runner_lock = threading.Lock()


def default_loop_runner() -> LoopRunner:
    """Return the process-wide runner, creating it on first use."""

    global _default_runner
    with _default_runner_lock:
        if _default_runner is None:
            _default_runner = LoopRunner()
            # A daemon thread parked in the selector is a poor thing to leave
            # for the interpreter to shoot down on the way out.
            atexit.register(_default_runner.stop)
        return _default_runner
