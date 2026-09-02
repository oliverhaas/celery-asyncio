# celery-asyncio

Distributed task queue with native asyncio support. An asyncio-native rewrite of [Celery](https://github.com/celery/celery), one of the most widely used distributed task systems in the Python ecosystem.

This project is **exploratory**. It is not affiliated with or endorsed by the Celery project. If you're looking for the official production-ready task queue, use the original [Celery](https://github.com/celery/celery).

## Features

- **Native asyncio worker** with hybrid thread pool for mixed async/sync workloads
- **`async def` tasks** run directly on the event loop, no thread overhead
- **Sync tasks** run in a thread pool alongside async tasks in the same worker
- **Valkey/Redis and AMQP transports** via the bundled asyncio `kombu` package
- **Familiar CLI** (`celery -A app worker`, `celery inspect`, `celery beat`), minus the flags that only applied to the prefork pool
- **Celery Flower** for monitoring, installed with `--no-deps` alongside the `flower` extra ([how](getting-started/quickstart.md))
- **Django 6.0 Tasks** support via [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
- **Targeting Python 3.14t** free-threading for true parallelism

## Quick example

```python
from celery import Celery

app = Celery("tasks", broker="redis://localhost:6379/0")

@app.task
async def add(x, y):
    return x + y

@app.task
def multiply(x, y):
    return x * y
```

```console
celery -A tasks worker --loglevel=info -E
```

Both `add` (async) and `multiply` (sync) run in the same worker. Async tasks run on the event loop, sync tasks run in a thread pool.

## Requirements

- Python 3.14+
- Valkey 8+ or Redis 7+ or RabbitMQ 4+

## License

BSD-3-Clause (same as Celery)

## Acknowledgments

This project builds on the ideas and design of [Celery](https://github.com/celery/celery) and [Kombu](https://github.com/celery/kombu) by Ask Solem and the Celery contributors.
