# celery-asyncio

Distributed task queue with native asyncio support. An asyncio-native rewrite of [Celery](https://github.com/celery/celery), one of the most widely used distributed task systems in the Python ecosystem.

This project is **exploratory**. It is not affiliated with or endorsed by the Celery project. If you're looking for the official production-ready task queue, use the original [Celery](https://github.com/celery/celery).

## Features

- **Native asyncio worker** with hybrid thread pool for mixed async/sync workloads
- **`async def` tasks** run directly on the event loop, no thread overhead
- **Sync tasks** run in a thread pool alongside async tasks in the same worker
- **Valkey/Redis and AMQP transports** via [kombu-asyncio](https://github.com/oliverhaas/kombu-asyncio)
- **Celery Flower** works out of the box for monitoring
- **Django 6.0 Tasks** integration via `CeleryBackend`
- **Targeting Python 3.14t** free-threading for true parallelism

## Quick Start

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

Both `add` (async) and `multiply` (sync) run in the same worker.

## Documentation

Full documentation at [oliverhaas.github.io/celery-asyncio](https://oliverhaas.github.io/celery-asyncio/)

## Requirements

- Python 3.14+
- kombu-asyncio 6.0+
- Valkey 8+ or Redis 7+ or RabbitMQ 4+

## Acknowledgments

This project builds on the ideas and design of [Celery](https://github.com/celery/celery) and [Kombu](https://github.com/celery/kombu) by Ask Solem and the Celery contributors.

## License

BSD-3-Clause
