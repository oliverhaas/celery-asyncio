# Architecture

## Worker model

The celery-asyncio worker uses a multi-threaded architecture:

```
Main thread
  |
  +-- Consumer event loop (asyncio)
  |     - Broker connection (Redis/AMQP)
  |     - Message dispatch
  |     - Timer/scheduler
  |
  +-- Loop worker threads (N)
  |     - Each runs its own asyncio event loop
  |     - Async tasks dispatched here
  |     - Semaphore limits concurrency per thread
  |
  +-- Sync worker threads (M)
        - ThreadPoolExecutor
        - Sync tasks dispatched here
```

### Main thread

Owns the broker connection and runs the consumer event loop. Messages are received here and dispatched to the appropriate worker thread based on whether the task is async or sync.

### Loop workers

Each loop worker runs its own asyncio event loop. Async tasks (`async def`) are scheduled directly on these loops. A semaphore controls how many tasks can run concurrently per loop worker.

The number of loop workers and per-worker concurrency are configurable:

```console
# 2 loop workers, 500 concurrent tasks each = 1000 total
celery -A app worker --loop-workers=2 --loop-concurrency=500
```

### Sync workers

Synchronous tasks run in a `ThreadPoolExecutor`. The number of sync worker threads is configurable:

```console
celery -A app worker --sync-workers=4
```

### Python 3.14t free-threading

With Python 3.14t (free-threaded build), all threads run with true parallelism since the GIL is disabled. This means sync tasks in the thread pool actually execute in parallel, not just concurrently.

## Consumer loop

The consumer loop in the main thread:

1. Drains the timer heap (fires scheduled entries like ETA/countdown tasks)
2. Blocks on `drain_events()` until a message arrives or timeout
3. Batch-drains remaining messages non-blocking to fill the concurrency pipeline
4. Checks restart conditions (max tasks, max memory, stuck threads)

## Bootsteps

The worker startup/shutdown sequence uses async bootsteps. Each step's `start()` and `stop()` methods are coroutines:

```python
class MyStep(bootsteps.Step):
    async def start(self, parent):
        ...

    async def stop(self, parent):
        ...
```

## Shutdown and restart

- **Signals**: `SIGTERM` and the first `SIGINT` start a warm shutdown. `SIGQUIT` or a second `SIGINT` starts a cold one, and so does `SIGTERM` with `REMAP_SIGTERM=SIGQUIT`. `SIGHUP` restarts a worker detached from its terminal, after a warm shutdown.
- **Warm shutdown**: the worker stops consuming and drops the prefetched tasks that have not started, leaving their messages for the broker to redeliver. Running async tasks get `worker_soft_shutdown_timeout` seconds to finish (0 by default). The ones still running after that are cancelled as a cold shutdown cancels them. Sync tasks are left to finish.
- **Cold shutdown**: the worker drops the prefetched tasks as a warm shutdown does, and cancels the running tasks immediately, sync ones as far as their thread can be stopped. An `acks_late` task is left unacknowledged for the broker to redeliver. Any other task was acknowledged when it started, so it is stored as `RETRY` and does not run again. A task whose body has returned is left to report its result.
- **Sync tasks**: a cold shutdown and `revoke(terminate=True)` stop a sync task by raising an exception in its thread, only while the task body runs. Python code stops immediately. A call blocked in C code, such as `time.sleep` or a socket read, sees the exception when it returns, and the worker waits for that before it closes the broker connection, as it waits for the sync tasks a warm shutdown leaves to finish.
- **Draining**: `worker_max_tasks_per_child`, `worker_max_memory_per_child` or a stuck thread makes the worker stop consuming and finish the tasks it already holds, prefetched ones included. When every sync thread is stuck, a task that has not started cannot start, so the worker finishes only the started ones and leaves the others for the broker to redeliver. It then runs its exit handlers, which save the `--statedb` file, and restarts itself with `os.execv`.
- **Hard time limit**: an async task is cancelled, and a sync task is stopped as above. Either is reported as failed. A sync task's thread still running 2 seconds later counts as stuck. Neither the restart that follows nor a shutdown waits for that thread, which the exec or the exit ends.
