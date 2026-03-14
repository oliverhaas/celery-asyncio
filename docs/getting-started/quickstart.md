# Quick Start

## 1. Start a broker

Valkey/Redis:

```console
docker run -d -p 6379:6379 valkey/valkey:8
```

Or RabbitMQ:

```console
docker run -d -p 5672:5672 -p 15672:15672 rabbitmq:4-management
```

## 2. Create your app

```python
# celeryapp.py
from celery import Celery

app = Celery("myapp")
app.config_from_object({
    "broker_url": "redis://localhost:6379/0",
    "result_backend": "redis://localhost:6379/1",
    "include": ["tasks"],
})
```

## 3. Define tasks

```python
# tasks.py
import asyncio
from celeryapp import app

@app.task
def add(x, y):
    return x + y

@app.task
async def slow_add(x, y):
    await asyncio.sleep(1)
    return x + y
```

Sync tasks (`add`) run in a thread pool. Async tasks (`slow_add`) run directly on the asyncio event loop.

## 4. Start the worker

```console
PYTHONPATH=. celery -A celeryapp worker --loglevel=info -E
```

The `-E` flag enables events for monitoring with Flower.

## 5. Send tasks

```python
from tasks import add, slow_add

# Fire and forget
add.delay(2, 3)

# With options
add.apply_async(args=(2, 3), countdown=10)

# Async task
slow_add.delay(10, 20)
```

## 6. Monitor with Flower (optional)

```console
pip install flower
celery -A celeryapp flower
```

Open [http://localhost:5555](http://localhost:5555) to see workers, tasks, and live graphs.

## Using RabbitMQ

Same setup, just change the broker URL:

```python
app.config_from_object({
    "broker_url": "amqp://guest:guest@localhost:5672//",
    "result_backend": "redis://localhost:6379/1",
    "include": ["tasks"],
})
```

The AMQP transport uses [aio-pika](https://github.com/mosquito/aio-pika) under the hood. Install it with:

```console
uv add aio-pika
```
