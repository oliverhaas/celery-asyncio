"""The consumer's async event loop.

In celery-asyncio, this is a thin wrapper around connection.drain_events().
The old Hub-based asynloop and blocking synloop are removed.
"""
import time

from celery.platforms import EX_OK
from celery.utils.log import get_logger
from celery.worker import state

logger = get_logger(__name__)

# How often to check memory (seconds).
_MEMORY_CHECK_INTERVAL = 5.0

# Module-level state for throttled checks.
_last_memory_check = 0.0


def _get_rss_kib() -> int:
    """Return current process RSS in KiB."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    try:
        import resource
        import sys

        rusage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            return rusage.ru_maxrss // 1024  # bytes -> KiB
        return rusage.ru_maxrss  # already KiB on Linux
    except Exception:
        return 0


def _trigger_restart(reason: str) -> None:
    """Register os.execv atexit handler and set should_stop."""
    import atexit

    from celery.apps.worker import _reload_current_worker

    logger.info("Worker restart: %s", reason)
    atexit.register(_reload_current_worker)
    state.should_stop = EX_OK


async def _enter_draining(consumer, reason: str) -> None:
    """Stop consuming new messages and reject/requeue prefetched tasks."""
    state.is_draining = True
    active_count = len(tuple(state.active_requests))
    logger.info(
        "Worker draining: %s. Stopped accepting new tasks, "
        "waiting for %d active task(s) to finish.",
        reason,
        active_count,
    )

    # Stop fetching new messages from the broker.
    try:
        await consumer.cancel()
    except Exception:
        pass

    # Reject/requeue any reserved-but-not-active requests back to the broker.
    reserved = set(state.reserved_requests)
    active = set(state.active_requests)
    prefetched = reserved - active
    for req in prefetched:
        try:
            req.reject(requeue=True)
            logger.debug("Requeued prefetched task %s[%s]", req.name, req.id)
        except Exception:
            pass


def _check_restart_conditions(obj, consumer, pool) -> bool:
    """Check if the worker should restart. Returns True if draining was initiated."""
    global _last_memory_check

    app = obj.app
    now = time.monotonic()

    # --- max_tasks_per_child ---
    max_tasks = app.conf.worker_max_tasks_per_child
    if max_tasks and state.all_total_count[0] >= max_tasks:
        if not state.is_draining:
            return True  # signal caller to enter draining
        if not tuple(state.active_requests):
            _trigger_restart(
                f"max tasks per child ({max_tasks}) reached"
            )
            return False

    # --- max_memory_per_child ---
    max_memory = app.conf.worker_max_memory_per_child
    if max_memory and (now - _last_memory_check) >= _MEMORY_CHECK_INTERVAL:
        _last_memory_check = now
        rss = _get_rss_kib()
        if rss > max_memory:
            if not state.is_draining:
                return True  # signal caller to enter draining
            if not tuple(state.active_requests):
                _trigger_restart(
                    f"memory limit exceeded (RSS {rss} KiB > {max_memory} KiB)"
                )
                return False

    # --- stuck threads ---
    if pool and getattr(pool, "_stuck_thread_count", 0) > 0:
        if not state.is_draining:
            return True  # signal caller to enter draining
        if not tuple(state.active_requests):
            _trigger_restart("stuck thread(s) detected after hard timeout")
            return False

    return False


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

    while blueprint.state == 1:  # RUN
        try:
            await connection.drain_events(timeout=1.0)
        except TimeoutError:
            pass
        except OSError:
            break

        # Check restart conditions (max_tasks, max_memory, stuck threads).
        should_drain = _check_restart_conditions(obj, consumer, pool)
        if should_drain and not state.is_draining:
            reason_parts = []
            max_tasks = obj.app.conf.worker_max_tasks_per_child
            if max_tasks and state.all_total_count[0] >= max_tasks:
                reason_parts.append(f"max tasks per child ({max_tasks}) reached")
            max_memory = obj.app.conf.worker_max_memory_per_child
            if max_memory:
                rss = _get_rss_kib()
                if rss > max_memory:
                    reason_parts.append(f"memory limit exceeded (RSS {rss} KiB > {max_memory} KiB)")
            if pool and getattr(pool, "_stuck_thread_count", 0) > 0:
                reason_parts.append("stuck thread(s) detected")
            await _enter_draining(consumer, "; ".join(reason_parts) or "limit reached")
