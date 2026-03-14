# RabbitMQ Example

Celery app using RabbitMQ (AMQP) as broker with Valkey/Redis as result backend.

## What it demonstrates

- AMQP transport via aio-pika (native asyncio)
- Basic task execution (sync and async)
- Native `async def` tasks on the asyncio event loop
- Delayed delivery via `countdown` and `eta`
- Task priority (AMQP 0-9)
- Automatic retries on failure
- Event monitoring via Flower
- RabbitMQ management UI

## Setup

```bash
# 1. Start RabbitMQ + Valkey/Redis
docker compose up -d

# 2. Install dependencies (from repo root)
cd ../..
uv venv && uv sync --group dev
uv pip install aio-pika
cd examples/rabbitmq

# 3. Start the worker (with -E to enable events)
PYTHONPATH=. uv run python -m celery -A celeryapp worker --loglevel=info -E

# 4. In another terminal, send tasks
PYTHONPATH=. uv run python run.py
```

## Load testing

```bash
PYTHONPATH=. uv run python load.py 1000        # 1000 mixed tasks
PYTHONPATH=. uv run python load.py 1000 --fast  # 1000 fast async tasks
```

## Flower dashboard

```bash
PYTHONPATH=. uv run python -m celery -A celeryapp flower
```

Open http://localhost:5555 to monitor workers and tasks.

## RabbitMQ management

Open http://localhost:15672 (guest/guest) to see queues, exchanges, and bindings.

## Cleanup

```bash
docker compose down
```
