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
| Messaging | kombu (sync) | kombu (asyncio, bundled) |
| Transport | AMQP, Redis, SQS, ... | Valkey/Redis, AMQP, Memory, Filesystem |
| Result backend | Redis, DB, memcached, ... | Valkey/Redis, Filesystem |
| Dependencies | billiard, vine, kombu | asgiref (kombu bundled) |
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
# The asyncio pool is the only one left, and it is the default
worker_pool = 'asyncio'

# valkey:// is new, redis:// still works
broker_url = 'valkey://localhost:6379/0'

# Same key, same meaning
result_backend = 'valkey://localhost:6379/1'
```

### Broker URL schemes

A URL scheme names a transport directly. The aliases upstream kept for the two
AMQP libraries are gone, so `pyamqp://` and `librabbitmq://` raise a
`ValueError` naming the schemes that do work:

| Scheme | Transport |
|--------|-----------|
| `amqp://`, `amqps://` | RabbitMQ, over aio-pika |
| `redis://`, `rediss://`, `valkey://`, `valkeys://` | Valkey or Redis |
| `filesystem://` | a directory of message files |
| `memory://` | in-process, for tests |

`pyamqp://guest@localhost//` becomes `amqp://guest@localhost//`.

### Removed settings

These settings no longer apply:

- `worker_pool` choices: `prefork`, `eventlet`, `gevent`, `solo` (only `asyncio`)
- Eventlet/gevent-specific settings
- `worker_autoscaler`, with the rest of autoscaling. The asyncio pool has no
  `grow()` or `shrink()`, so there was nothing for an autoscaler to drive
- The prefork settings `worker_timer`, `worker_timer_precision`,
  `worker_pool_putlocks`, `worker_lost_wait`, `worker_proc_alive_timeout` and
  `worker_eta_task_limit`, and `worker_agent`, which named a class nothing
  loaded
- `worker_detect_quorum_queues` and `worker_disable_prefetch`
- The broker settings the URL carries: `broker_port`, `broker_user`,
  `broker_password`, `broker_vhost`, `broker_login_method`,
  `broker_failover_strategy`, `broker_pool_limit` (there is no producer pool;
  a connection belongs to the loop that opened it) and
  `broker_native_delayed_delivery_queue_type`
- `result_compression`, `result_exchange` and `result_exchange_type`

`broker_use_ssl` and `broker_transport` are still accepted, and still ignored.
TLS and the transport come from the broker URL: `amqps://` or `rediss://`, with
the certificate paths in the URL query or in `broker_transport_options`.

### Settings that mean something different

`worker_prefetch_multiplier` still applies. The worker multiplies it by the
concurrency to get the initial prefetch count and passes that to the transport,
same as upstream. What the count then means depends on the broker: on AMQP it is
the broker's cap on unacknowledged messages, while Valkey and Redis cannot push,
so there it is the batch size one consume round-trip claims into a local buffer.
On those brokers it bounds the buffer rather than the number of unacknowledged
messages. The default is 4.

## Worker startup

The CLI is the same:

```bash
# Before
celery -A myapp worker --loglevel=info

# After -- identical
celery -A myapp worker --loglevel=info
```

The `-P` flag only accepts `asyncio` (or omit it -- it's the default).

The worker is a single process, so the node name substitutions `%i` and `%I`
always expand to `0` and to the empty string.

### Removed worker options

- `-O` / `--optimization`: the `fair` profile only ever described the prefork
  pool, and nothing read the value.
- `--disable-prefetch`: prefetching is bounded by the asyncio pool's semaphore.
- `--autoscale`: it only ever pinned concurrency to the low end of the range,
  since the asyncio pool cannot grow or shrink.

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

Flower works, but has to be installed with `--no-deps`: it requires upstream
`celery` from PyPI, which would install over the `celery` package this
distribution provides. The `flower` extra carries Flower's other dependencies,
so install the two together:

```bash
pip install "celery-asyncio[flower]"
pip install flower --no-deps
```

Or with uv:

```bash
uv sync --extra flower
uv pip install flower --no-deps
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
