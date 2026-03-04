# celery-asyncio

Distributed task queue with native asyncio support. A ground-up rewrite of [Celery](https://github.com/celery/celery) for modern Python.

## Features

- Native asyncio worker with hybrid thread pool
- Async and sync task support in the same worker
- Built on [kombu-asyncio](https://github.com/oliverhaas/kombu) for async messaging
- Redis, Memory, and Filesystem transports
- Full CLI compatibility (`celery -A app worker`, `celery inspect`, etc.)
- Targeting Python 3.14t free-threading for true parallelism

## Requirements

- Python 3.12+
- kombu-asyncio 6.0+

## Installation

```bash
pip install celery-asyncio
```

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

```bash
celery -A tasks worker --loglevel=info
```

## Architecture

The worker uses a multi-threaded architecture:

- **Main thread**: Owns the broker connection, runs the consumer event loop
- **Loop worker threads** (N): Each runs its own asyncio event loop for async tasks, with a semaphore limiting concurrency
- **Sync worker threads** (M): ThreadPoolExecutor for synchronous tasks

With Python 3.14t free-threading, all threads run with true parallelism (no GIL).

## License

BSD-3-Clause
