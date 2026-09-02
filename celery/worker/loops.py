"""The consumer's async event loop.

In celery-asyncio, this is a thin wrapper around connection.drain_events().
The old Hub-based asynloop and blocking synloop are removed.
"""

import asyncio
import sys
import time

from celery.bootsteps import RUN
from celery.platforms import EX_OK
from celery.utils.log import get_logger
from celery.worker import state
from celery.worker.state import maybe_shutdown

logger = get_logger(__name__)

# How often to check memory (seconds).
_MEMORY_CHECK_INTERVAL = 5.0

# Most messages the inner drain may take before the outer loop gets a turn.
# Without a cap a deep backlog starves the restart checks below.
_MAX_DRAIN_BATCH = 1000

# Guard: only register the atexit restart handler once. Process-global on
# purpose -- atexit is process-global, and a restart execs over this process.
_restart_registered = False


def _get_rss_kib() -> int:
    """Return the current process RSS in KiB, or 0 where it cannot be read."""
    # /proc/self/status is the current RSS. The getrusage fallback below is
    # the high-water mark, so off procfs this over-estimates.
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError, ValueError:
        pass
    try:
        import resource
    except ImportError:
        return 0
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return rusage.ru_maxrss // 1024  # bytes -> KiB
    return rusage.ru_maxrss  # KiB on Linux


def _trigger_restart(reason: str) -> None:
    """Register os.execv atexit handler and set should_stop."""
    global _restart_registered

    if _restart_registered:
        return

    import atexit

    from celery.apps.worker import _reload_current_worker

    logger.info("Worker restart: %s", reason)
    atexit.register(_reload_current_worker)
    _restart_registered = True
    state.should_stop = EX_OK


async def _enter_draining(consumer, reason: str) -> None:
    """Stop consuming new messages and let the tasks already in hand finish."""
    state.is_draining = True
    with state._lock:
        unfinished = len(state.reserved_requests)
    logger.info(
        "Worker draining: %s. Stopped accepting new tasks, waiting for %d task(s) in hand to finish.",
        reason,
        unfinished,
    )

    # Stop fetching new messages from the broker.
    await consumer.cancel()

    # Prefetched tasks stay here: once the pool has a job, rejecting the message
    # would run it twice. The drain waits on the reserved set, running and queued alike.


def _check_restart_conditions(obj, pool) -> str | None:
    """Check if the worker should restart.

    Returns a reason string if draining should be initiated (or restart
    triggered), or None if nothing to do.
    """
    app = obj.app
    now = time.monotonic()

    # Reserved covers both the running tasks and the ones still queued behind
    # the pool's concurrency limit.
    if state.is_draining:
        with state._lock:
            unfinished = bool(state.reserved_requests)
        if not unfinished:
            _trigger_restart("all tasks finished during drain")
        return None

    # Build reason parts for conditions that require draining + restart.
    reason_parts = []

    # --- max_tasks_per_child ---
    max_tasks = app.conf.worker_max_tasks_per_child
    if max_tasks and state.all_total_count[0] >= max_tasks:
        reason_parts.append(f"max tasks per child ({max_tasks}) reached")

    # --- max_memory_per_child ---
    max_memory = app.conf.worker_max_memory_per_child
    if max_memory and (now - getattr(obj, "_last_memory_check", 0.0)) >= _MEMORY_CHECK_INTERVAL:
        obj._last_memory_check = now
        rss = _get_rss_kib()
        if rss > max_memory:
            reason_parts.append(f"memory limit exceeded (RSS {rss} KiB > {max_memory} KiB)")

    # --- stuck threads ---
    if pool and getattr(pool, "_stuck_thread_count", 0) > 0:
        reason_parts.append("stuck thread(s) detected after hard timeout")

    return "; ".join(reason_parts) if reason_parts else None


async def asynloop(
    obj, connection, consumer, blueprint, qos=None, amqheartbeat=None, clock=None, amqheartbeat_rate=None, **kwargs
):
    """Async consumer event loop.

    Drains events from the broker connection using native asyncio.

    Called with arguments from Consumer.loop_args():
        obj: Consumer instance
        connection: broker connection
        consumer: task consumer (kombu Consumer)
        blueprint: consumer blueprint
        qos: QoS manager
        amqheartbeat: AMQP heartbeat interval
        clock: Lamport clock
        amqheartbeat_rate: heartbeat check rate
    """
    # Create the task message handler and register it on the consumer.
    # kombu callbacks receive (body, message) but celery's handler expects (message).
    on_task_received = obj.create_task_handler()
    consumer.register_callback(lambda body, message: on_task_received(message))
    await consumer.consume()

    # Notify that the consumer is ready
    obj.on_ready()

    pool = getattr(obj, "pool", None)
    timer = obj.timer

    while blueprint.state == RUN:
        maybe_shutdown()

        # Push out prefetch changes the strategy and the ETA timer only recorded,
        # or an ETA task's increment_eventually() holds a slot for the worker's lifetime.
        if qos is not None and qos.prev != qos.value:
            await qos.update()

        # Drain the timer: fire any scheduled entries whose ETA has passed
        # (e.g. countdown/eta tasks, rate-limit buckets).
        drain_timeout = 1.0
        while True:
            delay, entry = next(timer)
            if entry is not None:
                timer.apply_entry(entry)
            else:
                # delay = time until next scheduled entry (or max_interval)
                drain_timeout = min(delay, 1.0)
                break

        try:
            # Block until at least one message arrives (or timeout).
            await connection.drain_events(timeout=drain_timeout)
            # Got one, now drain remaining available messages non-blocking
            # to fill the concurrency pipeline.
            batch = 0
            while blueprint.state == RUN and batch < _MAX_DRAIN_BATCH:
                try:
                    await connection.drain_events(timeout=0)
                    batch += 1
                    # Yield to the event loop periodically so other coroutines
                    # (ack/reject, timer callbacks) get a chance to run.
                    if batch % 100 == 0:
                        await asyncio.sleep(0)
                except TimeoutError:
                    break
        except TimeoutError:
            pass
        except OSError:
            # A broken broker socket. Letting it out reaches the recoverable-error
            # handler, which stops the running bootsteps; a break here stacked a second set.
            if blueprint.state == RUN:
                raise
            break

        # Check restart conditions (max_tasks, max_memory, stuck threads).
        drain_reason = _check_restart_conditions(obj, pool)
        if drain_reason and not state.is_draining:
            await _enter_draining(consumer, drain_reason)
