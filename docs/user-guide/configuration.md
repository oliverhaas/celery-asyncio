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

### Transport options

`result_backend_transport_options` is passed through to the backend. The
Valkey/Redis backend reads one key of its own:

| Key | Default | Description |
|-----|---------|-------------|
| `additional_connection_errors` | `()` | Extra exception classes, or dotted paths to them, to treat as connection errors and retry |

A proxy or a managed Valkey/Redis service in front of the server raises its own
exception type when it drops a connection, and the retry machinery only knows
the client library's types. List those here so they are retried instead of
surfacing as a hard failure. An entry that does not resolve to an exception
class is skipped with a warning rather than failing backend construction:

```python
app.config_from_object({
    "result_backend_transport_options": {
        "additional_connection_errors": ["mypackage.errors.ProxyDisconnected"],
    },
})
```

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
| `worker_soft_shutdown_timeout` | 0.0 | Seconds to let active tasks finish before cancelling them on shutdown; 0 cancels straight away |
| `worker_enable_soft_shutdown_on_idle` | False | Wait out `worker_soft_shutdown_timeout` even with no active tasks |
| `worker_deduplicate_successful_tasks` | False | On a redelivered message, look the task id up in the result backend first and skip it if it already succeeded. Needs `task_acks_late` and a persistent backend |

## Acknowledgement

`task_acks_late` moves the acknowledgement to after the task has run, so a task
whose worker dies mid-execution is redelivered. These settings decide what
happens to a message when the task that ran late-acknowledged did not succeed:

| Setting | Default | Description |
|---------|---------|-------------|
| `task_acks_on_failure` | None | Acknowledge the message when the task raises |
| `task_acks_on_timeout` | None | Acknowledge the message when the task hits its time limit |
| `task_acks_on_failure_or_timeout` | True | Covers both of the above; deprecated in 6.0, to be removed in 7.0 |

`None` means "fall back to `task_acks_on_failure_or_timeout`", so the default
behaviour is unchanged from upstream: a failed or timed-out task is
acknowledged and not redelivered. Set one of the two to `False` to have that
kind of outcome redelivered instead. All three only apply to tasks that are
acknowledged late; without `task_acks_late` the message is already gone by the
time the task runs. The per-task attributes `acks_on_failure` and
`acks_on_timeout` override the app setting.

## Prefetch

| Setting | CLI flag | Default | Description |
|---------|----------|---------|-------------|
| `worker_prefetch_multiplier` | `--prefetch-multiplier` | 4 | Multiplied by the concurrency to get the prefetch count the worker asks the transport for |
| `worker_enable_prefetch_count_reduction` | | True | After a connection loss, reconnect with a prefetch count reduced by the number of tasks still running |

What the prefetch count means depends on the broker. On AMQP it is the usual
cap on unacknowledged messages. Valkey and Redis cannot push, so there it is
the batch size one consume round-trip claims into a local buffer: it bounds the
buffer, not the number of unacknowledged messages.

With `worker_enable_prefetch_count_reduction` on, a worker that reconnects
while N tasks are still running comes back with a lower count and restores the
full one as those tasks finish, so it does not claim a second full batch on top
of the work already in hand.

## Connection loss

| Setting | Default | Description |
|---------|---------|-------------|
| `worker_cancel_long_running_tasks_on_connection_loss` | False | On losing the broker connection, cancel the running tasks that have `task_acks_late` set |

A late-acknowledged task cannot be acknowledged once the connection is gone, so
the broker redelivers it and the work is done twice. Turning this on cancels
those tasks instead and lets the redelivery be the only run. It is off by
default, and leaving it off logs a pending-deprecation warning on every
reconnect.

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
  --prefetch-multiplier=N  Prefetch count per concurrency slot (default: 4)
```
