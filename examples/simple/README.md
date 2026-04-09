# Simple Example

Minimal Celery app using celery-asyncio.

## What it demonstrates

- Basic task execution (sync and async)
- Native `async def` tasks on the asyncio event loop
- Delayed delivery via `countdown` and `eta`
- Task priority (0-255)
- Automatic retries on failure
- Event monitoring via Flower

## Setup

```bash
# 1. Start Valkey/Redis
docker compose up -d

# 2. Install dependencies (from repo root)
cd ../..
uv venv && uv sync --group dev
cd examples/simple

# 3. Start the worker (with -E to enable events)
PYTHONPATH=. uv run python -m celery -A celeryapp worker --loglevel=info -E

# 4. In another terminal, send tasks
PYTHONPATH=. uv run python run.py
```

## Expected output

```
============================================================
celery-asyncio example
============================================================

1) Basic task: add(2, 3)
   result = 5

2) Async task: slow_add(10, 20) - runs on the event loop
   result = 30

3) Delayed task: add(10, 20) with countdown=3s
   waiting for delayed result...
   result = 30

4) ETA task: multiply(6, 7) with eta=...
   waiting for eta result...
   result = 42

5) Priority tasks: three add() calls with different priorities
   low  (priority=0):   2
   mid  (priority=128): 4
   high (priority=255): 6

6) Flaky task (retries up to 3 times):
   result = succeeded after retries

============================================================
All tasks completed successfully!
============================================================
```

## Flower dashboard (optional)

```bash
# Install Flower (note: reinstall celery-asyncio + kombu-asyncio afterwards
# because flower pulls in the upstream versions from PyPI)
uv pip install flower
uv pip install -e ../.. && uv pip install -e ../../../kombu

# Start Flower
PYTHONPATH=. uv run python -m celery -A celeryapp flower
```

Open http://localhost:5555 in your browser while running tasks. You should see:

- **Workers tab** - the worker and its status
- **Tasks tab** - each task as it's sent, received, and completed
- **Monitor tab** - live graphs of task throughput and latency

## Cleanup

```bash
docker compose down
```
