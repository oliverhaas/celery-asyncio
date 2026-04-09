# Migration Guide

How to migrate from upstream Celery to celery-asyncio.

!!! warning "Alpha software"

    celery-asyncio is in alpha. APIs may change between releases.
    This guide covers the current state of the project.

## What changed

celery-asyncio is a ground-up asyncio rewrite. The worker, transport layer,
and concurrency model are completely different from upstream Celery.

| Area | Upstream Celery | celery-asyncio |
|------|----------------|----------------|
| Python | 3.8+ | 3.14+ only |
| Concurrency | prefork, eventlet, gevent, threads | asyncio + threads |
| Messaging | kombu (sync) | kombu-asyncio (pure async) |
| Transport | AMQP, Redis, SQS, ... | Valkey/Redis, AMQP, Memory, Filesystem |
| Result backend | Redis, DB, memcached, ... | Valkey/Redis, Filesystem |
| Dependencies | billiard, vine, kombu | kombu-asyncio, asgiref |
| Task types | sync only (async via eventlet/gevent) | native `async def` + sync |

## Installation

Replace `celery` with `celery-asyncio`:

```bash
# Before
pip install celery[redis]

# After
pip install celery-asyncio[redis]
# or
pip install celery-asyncio[valkey]
```

## Task definitions

**Sync tasks work unchanged.** No code changes needed:

```python
@app.task
def add(x, y):
    return x + y
```

**Async tasks are now native.** No eventlet/gevent needed:

```python
@app.task
async def fetch_url(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()
```

Async tasks run directly on the asyncio event loop. Sync tasks run in a
thread pool. Both can coexist in the same worker.

## Configuration

Most configuration keys are the same. Key differences:

```python
# Worker pool -- only 'asyncio' is supported
# (prefork, eventlet, gevent are removed)
worker_pool = 'asyncio'          # default, no need to set

# Broker URL -- valkey:// scheme now supported
broker_url = 'valkey://localhost:6379/0'  # or redis:// still works

# Result backend -- same
result_backend = 'valkey://localhost:6379/1'  # or redis://
```

### Removed settings

These settings no longer apply:

- `worker_pool` choices: `prefork`, `eventlet`, `gevent`, `solo` (only `asyncio`)
- `worker_prefetch_multiplier` (asyncio pool uses semaphore-based concurrency)
- Eventlet/gevent-specific settings

## Worker startup

The CLI is the same:

```bash
# Before
celery -A myapp worker --loglevel=info

# After -- identical
celery -A myapp worker --loglevel=info
```

The `-P` flag only accepts `asyncio` (or omit it -- it's the default).

## Canvas primitives

`chain`, `group`, `chord`, `chunks` work the same way:

```python
from celery import chain, group, chord

# All of these work as before
chain(add.s(1, 2), add.s(3)).delay()
group(add.s(i, i) for i in range(10)).delay()
chord(group(add.s(i, i) for i in range(10)), add.s()).delay()
```

Canvas also supports async dispatch via `aapply_async()` and `adelay()`.

## Result retrieval

`AsyncResult` works the same:

```python
result = add.delay(2, 3)
meta = app.backend.get_task_meta(result.id)
```

The result backend supports native async operations (`aget_task_meta()`,
`astore_result()`, etc.) using the async Redis client.

## Monitoring with Flower

Flower works but requires special installation since it depends on
upstream `celery` from PyPI:

```bash
pip install flower --no-deps
pip install celery-asyncio[flower]
```

Or with uv (handles the dependency conflict automatically):

```bash
uv sync --extra flower
```

Then start as usual:

```bash
celery -A myapp flower
```

## What's removed

- prefork pool, replaced by asyncio + thread pool
- eventlet/gevent, native `async def` replaces green threads
- billiard, no longer needed (no forking)
- vine, promises replaced by asyncio futures
- SQS, Zookeeper, Consul transports, not yet ported
- Database, Memcached, S3 result backends, not yet ported

## What's new

- Native async tasks: `async def` tasks run on the event loop
- Valkey support: first-class `valkey://` URL scheme
- AMQP via aio-pika: native asyncio RabbitMQ support
- Python 3.14 only, uses latest language features
- Free-threading ready, designed for Python 3.14t
