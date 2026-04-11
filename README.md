# celery-asyncio

Distributed task queue with native asyncio support. An asyncio-native rewrite of [Celery](https://github.com/celery/celery), one of the most widely used distributed task systems in the Python ecosystem.

This project is **exploratory**. It is not affiliated with or endorsed by the Celery project. If you're looking for the official production-ready task queue, use the original [Celery](https://github.com/celery/celery).

## Features

- **Native asyncio worker** with hybrid thread pool for mixed async/sync workloads
- **`async def` tasks** run directly on the event loop, no thread overhead
- **Sync tasks** run in a thread pool alongside async tasks in the same worker
- **Valkey/Redis and AMQP transports** via [kombu-asyncio](https://github.com/oliverhaas/kombu-asyncio)
- **Celery Flower** works out of the box for monitoring
- **Django 6.0 Tasks** support via [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery)
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

## Attribution

This project is an asyncio rewrite of [Celery](https://github.com/celery/celery) by Ask Solem, Asif Saif Uddin & contributors. Much of the utility layer (`celery/utils/`, `celery/loaders/`, `celery/bin/`), scheduling (`celery/schedules.py`, `celery/beat.py`), data structures (`celery/canvas.py`, `celery/result.py`, `celery/states.py`), and app framework (`celery/app/`) are carried over from the original with targeted modifications. The worker event loop (`celery/worker/loops.py`), asyncio pool (`celery/concurrency/aio.py`), promise system (`celery/utils/promises.py`), and timer/hub (`celery/utils/scheduling.py`) were written from scratch. Files containing substantial original Celery code are marked with a header comment.

## License

BSD-3-Clause
