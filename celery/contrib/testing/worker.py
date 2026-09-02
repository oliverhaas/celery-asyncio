"""Embedded workers for integration tests."""

import asyncio
import concurrent.futures
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import celery.worker.consumer  # noqa
from celery import Celery, worker
from celery.result import _set_task_join_will_block, allow_join_result
from celery.utils.dispatch import Signal
from celery.utils.eventloop import default_loop_runner
from celery.utils.nodenames import anon_nodename

WORKER_LOGLEVEL = os.environ.get("WORKER_LOGLEVEL", "error")

#: Seconds to wait for the embedded worker to reach the ready callback.
STARTUP_TIMEOUT = 30.0

#: How often the startup wait looks at the worker coroutine while waiting.
_STARTUP_POLL_INTERVAL = 0.05

test_worker_starting = Signal(
    name="test_worker_starting",
    providing_args={},
)
test_worker_started = Signal(
    name="test_worker_started",
    providing_args={"worker", "consumer"},
)
test_worker_stopped = Signal(
    name="test_worker_stopped",
    providing_args={"worker"},
)


class TestWorkController(worker.WorkController):
    """Worker that can synchronize on being fully started."""

    # When this class is imported in pytest files, prevent pytest from thinking
    # this is a test class
    __test__ = False

    class Blueprint(worker.WorkController.Blueprint):
        """Blueprint that keeps hold of whatever ended the worker.

        `WorkController.start` turns an unrecoverable error into an exit code,
        which is what a worker process wants. An embedded worker has a caller
        blocked on the ready callback instead, and that callback will never
        fire, so the error has to be kept for that caller to raise.
        """

        async def start(self, parent):
            try:
                await super().start(parent)
            except BaseException as exc:
                parent.worker_error = exc
                raise

    def __init__(self, *args, **kwargs):
        self._on_started = threading.Event()
        #: The exception that ended the blueprint, if one did.
        self.worker_error: BaseException | None = None
        super().__init__(*args, **kwargs)

    def on_consumer_ready(self, consumer):
        """Callback called when the Consumer blueprint is fully started."""
        self._on_started.set()
        test_worker_started.send(sender=self.app, worker=self, consumer=consumer)

    def ensure_started(self, timeout: float | None = None) -> bool:
        """Wait for the worker to be fully up and running.

        Returns whether it got there before `timeout` ran out. The worker has
        to be started on another thread or loop for this to be reachable.
        """
        return self._on_started.wait(timeout)


@contextmanager
def start_worker(
    app,
    concurrency=1,
    pool="asyncio",
    loglevel=WORKER_LOGLEVEL,
    logfile=None,
    perform_ping_check=True,
    ping_task_timeout=10.0,
    shutdown_timeout=10.0,
    startup_timeout=STARTUP_TIMEOUT,
    **kwargs,
):
    """Start embedded worker.

    Yields:
        celery.app.worker.Worker: worker instance.
    """
    test_worker_starting.send(sender=app)

    worker = None
    try:
        with _start_worker_thread(
            app,
            concurrency=concurrency,
            pool=pool,
            loglevel=loglevel,
            logfile=logfile,
            perform_ping_check=perform_ping_check,
            shutdown_timeout=shutdown_timeout,
            startup_timeout=startup_timeout,
            **kwargs,
        ) as worker:
            if perform_ping_check:
                from .tasks import ping

                with allow_join_result():
                    assert ping.delay().get(timeout=ping_task_timeout) == "pong"

            yield worker
    finally:
        test_worker_stopped.send(sender=app, worker=worker)


@contextmanager
def _start_worker_thread(
    app: Celery,
    concurrency: int = 1,
    pool: str = "asyncio",
    loglevel: str | int = WORKER_LOGLEVEL,
    logfile: str | None = None,
    WorkController: Any = TestWorkController,
    perform_ping_check: bool = True,
    shutdown_timeout: float = 10.0,
    startup_timeout: float = STARTUP_TIMEOUT,
    **kwargs,
) -> Iterator[worker.WorkController]:
    """Start Celery worker in a thread.

    Yields:
        celery.worker.Worker: worker instance.
    """
    setup_app_for_worker(app, loglevel, logfile)
    if perform_ping_check:
        assert "celery.ping" in app.tasks

    w = WorkController(
        app=app,
        concurrency=concurrency,
        hostname=kwargs.pop("hostname", anon_nodename()),
        pool=pool,
        loglevel=loglevel,
        logfile=logfile,
        # not allowed to override TestWorkController.on_consumer_ready
        ready_callback=None,
        without_heartbeat=kwargs.pop("without_heartbeat", True),
        without_mingle=True,
        without_gossip=True,
        **kwargs,
    )

    # worker.start() is async. It runs on the process-wide background loop, not
    # a private one: the test publishes through that same loop, and a transport
    # object cannot be shared across two of them.
    running = asyncio.run_coroutine_threadsafe(w.start(), default_loop_runner().loop)
    _wait_until_ready(w, running, startup_timeout)
    _set_task_join_will_block(False)

    try:
        yield w
    finally:
        from celery.worker import state

        state.should_terminate = 0
        try:
            running.result(timeout=shutdown_timeout)
        except concurrent.futures.TimeoutError:
            running.cancel()
            raise RuntimeError(
                "Worker failed to exit within the allocated timeout. "
                "Consider raising `shutdown_timeout` if your tasks take longer "
                "to execute."
            ) from None
        finally:
            state.should_terminate = None


def _wait_until_ready(
    w: TestWorkController,
    running: concurrent.futures.Future,
    timeout: float,
) -> None:
    """Block until the worker is ready, gave up, or ran out of time.

    Waiting on the ready callback alone meant a worker whose blueprint raised
    on the way up never woke the caller: `start_worker` sat in `ensure_started`
    for good, and none of its cleanup ran.
    """
    deadline = time.monotonic() + timeout
    while not w.ensure_started(_STARTUP_POLL_INTERVAL):
        if running.done():
            # Re-raises whatever start() itself raised, on this thread.
            running.result()
            if w.worker_error is not None:
                raise w.worker_error
            raise RuntimeError("Embedded worker stopped before it was ready.")
        if time.monotonic() >= deadline:
            running.cancel()
            raise RuntimeError(f"Embedded worker was not ready within {timeout} seconds.")


def setup_app_for_worker(app: Celery, loglevel: str | int, logfile: str | None = None) -> None:
    """Setup the app to be used for starting an embedded worker.

    `logfile` of None means log to stderr; an empty string would be taken for a
    filename and open the working directory.
    """
    app.finalize()
    app.set_current()
    app.set_default()
    type(app.log)._setup = False
    app.log.setup(loglevel=loglevel, logfile=logfile)
