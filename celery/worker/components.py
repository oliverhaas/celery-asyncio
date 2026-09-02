# Partially from Celery by Ask Solem & contributors (BSD-3-Clause)
# https://github.com/celery/celery
"""Worker-level Bootsteps.

Simplified for asyncio - no Hub, no prefork/eventlet/gevent pools.
"""

import asyncio
import atexit
import threading
import time

from celery import bootsteps
from celery._state import _set_task_join_will_block
from celery.utils.log import get_logger

logger = get_logger(__name__)

__all__ = ("Timer", "Pool", "Beat", "StateDB", "Consumer", "stop_pool")

#: Longest the worker waits for the pool to bring its threads down.
POOL_STOP_TIMEOUT = 30.0


async def stop_pool(pool, timeout=POOL_STOP_TIMEOUT):
    """Stop `pool` on a helper thread, waiting at most `timeout` seconds.

    Returns whether it finished in time. The pool joins each of its threads
    with a timeout of its own, and doing that on the event loop thread stalls
    everything else the worker has left to do, up to ten seconds per thread.
    """
    stopped = threading.Event()

    def stop():
        try:
            pool.stop()
        finally:
            stopped.set()

    threading.Thread(target=stop, name="celery-pool-stop", daemon=True).start()

    deadline = time.monotonic() + timeout
    while not stopped.is_set():
        if time.monotonic() >= deadline:
            logger.warning("Pool did not stop within %ss, leaving its threads behind", timeout)
            return False
        await asyncio.sleep(0.05)
    return True


class Timer(bootsteps.Step):
    """Timer bootstep.

    Provides a lightweight timer for ETA task scheduling.
    """

    def create(self, w):
        from celery.utils.scheduling import Timer as _Timer

        w.timer = _Timer(max_interval=10.0)


class Pool(bootsteps.StartStopStep):
    """Bootstep managing the worker pool.

    In celery-asyncio, this is a hybrid asyncio + ThreadPoolExecutor pool.
    Async tasks run on the event loop, sync tasks run in threads.

    Adds the ``pool`` attribute.
    """

    requires = (Timer,)

    def __init__(self, w, **kwargs):
        w.pool = None
        super().__init__(w, **kwargs)

    def close(self, w):
        if w.pool:
            w.pool.close()

    async def stop(self, w):
        if w.pool:
            await stop_pool(w.pool)

    async def terminate(self, w):
        if w.pool:
            w.pool.terminate()
            await stop_pool(w.pool)

    def create(self, w):
        procs = w.concurrency
        w.process_task = w._process_task
        pool = w.pool = self.instantiate(
            w.pool_cls,
            procs,
            initargs=(w.app, w.hostname),
            maxtasksperchild=w.max_tasks_per_child,
            max_memory_per_child=w.max_memory_per_child,
            timeout=w.time_limit,
            soft_timeout=w.soft_time_limit,
            loop_workers=w.loop_workers,
            loop_concurrency=w.loop_concurrency,
            sync_workers=w.sync_workers,
            app=w.app,
        )
        _set_task_join_will_block(pool.task_join_will_block)
        return pool

    def info(self, w):
        return {"pool": w.pool.info if w.pool else "N/A"}


class Beat(bootsteps.StartStopStep):
    """Step used to embed a beat process.

    Enabled when the ``beat`` argument is set.
    """

    label = "Beat"
    conditional = True

    def __init__(self, w, beat=False, **kwargs):
        self.enabled = w.beat = beat
        w.beat = None
        super().__init__(w, beat=beat, **kwargs)

    def create(self, w):
        from celery.beat import EmbeddedService

        b = w.beat = EmbeddedService(w.app, schedule_filename=w.schedule_filename, scheduler_cls=w.scheduler)
        return b


class StateDB(bootsteps.Step):
    """Bootstep that sets up between-restart state database file."""

    def __init__(self, w, **kwargs):
        self.enabled = w.statedb
        w._persistence = None
        super().__init__(w, **kwargs)

    def create(self, w):
        w._persistence = w.state.Persistent(w.state, w.statedb, w.app.clock)
        atexit.register(w._persistence.save)


class Consumer(bootsteps.StartStopStep):
    """Bootstep starting the Consumer blueprint."""

    last = True

    def create(self, w):
        prefetch_count = w.concurrency * w.prefetch_multiplier
        c = w.consumer = self.instantiate(
            w.consumer_cls,
            w.process_task,
            hostname=w.hostname,
            task_events=w.task_events,
            init_callback=w.ready_callback,
            initial_prefetch_count=prefetch_count,
            pool=w.pool,
            timer=w.timer,
            app=w.app,
            controller=w,
            worker_options=w.options,
            disable_rate_limits=w.disable_rate_limits,
            prefetch_multiplier=w.prefetch_multiplier,
        )
        return c
