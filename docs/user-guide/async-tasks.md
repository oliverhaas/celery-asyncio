# Async Tasks

## Defining async tasks

Any task decorated with `@app.task` can be an `async def`:

```python
import asyncio
from celery import Celery

app = Celery("myapp", broker="redis://localhost:6379/0")

@app.task
async def fetch_url(url):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()
```

Async tasks run directly on the asyncio event loop without any thread overhead. They can use `await`, `async with`, `async for`, and any asyncio-compatible library.

## Mixing async and sync tasks

Both async and sync tasks can coexist in the same worker:

```python
@app.task
async def async_task(x):
    await asyncio.sleep(1)
    return x * 2

@app.task
def sync_task(x):
    import time
    time.sleep(1)
    return x * 2
```

The worker automatically routes each task to the right executor:

- `async def` tasks go to loop worker threads (asyncio event loops)
- Regular `def` tasks go to sync worker threads (thread pool)

## Concurrency

Async tasks can achieve high concurrency because they don't block threads. A single loop worker can run hundreds of async tasks concurrently:

```console
# 1 loop worker with 1000 concurrent tasks
celery -A app worker --loop-workers=1 --loop-concurrency=1000
```

This is useful for I/O-bound workloads like HTTP requests, database queries, or message publishing.

## Task timeouts

Timeouts work for both async and sync tasks:

```python
@app.task(soft_time_limit=30, time_limit=60)
async def long_task():
    ...
```

- Async tasks: cancelled via `asyncio.timeout`
- Sync tasks: interrupted via `threading.Timer`

## Bound tasks

Bound tasks work the same as upstream Celery:

```python
@app.task(bind=True, max_retries=3)
async def retrying_task(self, url):
    try:
        return await fetch(url)
    except ConnectionError:
        raise self.retry(countdown=5)
```

## Delayed and scheduled tasks

```python
from datetime import datetime, timedelta, UTC

# Countdown (seconds from now)
async_task.apply_async(args=(42,), countdown=30)

# ETA (specific time)
eta = datetime.now(tz=UTC) + timedelta(hours=1)
async_task.apply_async(args=(42,), eta=eta)
```

## Priority

Task priority is supported on both Redis and AMQP transports:

```python
# Higher number = executed sooner (0-255 for Redis, 0-9 for AMQP)
async_task.apply_async(args=(42,), priority=255)
```
