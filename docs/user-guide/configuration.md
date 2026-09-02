# Configuration

## Broker

```python
app.config_from_object({
    # Valkey/Redis
    "broker_url": "redis://localhost:6379/0",  # or valkey://

    # RabbitMQ (AMQP)
    # "broker_url": "amqp://guest:guest@localhost:5672//",
})
```

## Result backend

```python
app.config_from_object({
    "result_backend": "redis://localhost:6379/1",
})
```

Without a result backend, task return values are discarded. Set `result_extended = True` to store extra metadata (task name, args, kwargs, worker hostname).

## Pool sizing

The asyncio pool is sized by three settings, not by `worker_concurrency`:

| Setting | CLI flag | Default | Description |
|---------|----------|---------|-------------|
| `worker_loop_workers` | `--loop-workers` | 1 | Threads running an event loop |
| `worker_loop_concurrency` | `--loop-concurrency` | 10 | Concurrent async tasks per loop worker |
| `worker_sync_workers` | `--sync-workers` | 1 | Threads for sync tasks |

Async task capacity is `worker_loop_workers × worker_loop_concurrency`. Sync tasks
run in a separate pool of `worker_sync_workers` threads. On Python 3.14t both pools
run in parallel.

`worker_concurrency` and `-c` are accepted for compatibility with upstream Celery
and size the prefetch count, but the asyncio pool does not read them for capacity.

## Worker settings

| Setting | Default | Description |
|---------|---------|-------------|
| `worker_max_tasks_per_child` | None | Restart worker after N tasks |
| `worker_max_memory_per_child` | None | Restart worker if RSS exceeds N KiB |
| `task_soft_time_limit` | None | Soft time limit in seconds |
| `task_time_limit` | None | Hard time limit in seconds |
| `task_acks_late` | False | Acknowledge tasks after execution |

## Serialization

```python
app.config_from_object({
    "task_serializer": "json",       # json, pickle, msgpack, yaml
    "result_serializer": "json",
    "accept_content": ["json"],
})
```

## Task autodiscovery

```python
app = Celery("myapp")
app.config_from_object({
    "include": ["myapp.tasks", "myapp.other_tasks"],
})
```

Or with Django:

```python
app.autodiscover_tasks()
```

## CLI options

```console
celery -A app worker [OPTIONS]

Options:
  --loglevel=INFO          Log level (DEBUG, INFO, WARNING, ERROR)
  -E / --task-events       Enable task events for Flower
  --loop-workers=N         Number of async loop workers (default: 1)
  --loop-concurrency=N     Max concurrent async tasks per loop worker (default: 10)
  --sync-workers=N         Number of sync worker threads (default: 1)
  -c / --concurrency=N     Accepted for compatibility; does not size the asyncio pool
  --max-tasks-per-child=N  Restart after N tasks
  --max-memory-per-child=N Restart if RSS exceeds N KiB
```
