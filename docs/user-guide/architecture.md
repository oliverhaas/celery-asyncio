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

- **Graceful shutdown**: `SIGTERM` or `SIGINT` sets `state.should_stop`, the consumer loop exits, active tasks finish
- **Draining**: stops consuming new messages, rejects/requeues prefetched tasks, waits for active tasks to complete
- **Restart**: `max_tasks_per_child` or `max_memory_per_child` triggers drain + `os.execv` restart
- **Hard timeout**: stuck sync tasks are cancelled via `threading.Timer`, stuck threads trigger worker restart
