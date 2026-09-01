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
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def run(self, coro: Coroutine[Any, Any, _R]) -> _R:
        """Run `coro` on the background loop, blocking until it returns."""

        if current_loop() is not None:
            coro.close()
            raise RuntimeError(
                "Cannot block on the background loop from inside a running event loop. "
                "Await the async form instead, for example asend_task() rather than send_task(), "
                "or adelay()/aapply_async() rather than delay()/apply_async()."
            )

        loop = self.loop
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
