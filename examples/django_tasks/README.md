# Django Tasks Example

Minimal Django 6.0 project using celery-asyncio as the task backend.

## What it demonstrates

- Django's `@task` decorator with celery-asyncio as the backend
- Sync and async task execution
- Delayed delivery via `run_after` (ETA)
- Task priority (-100..100)
- `takes_context=True` for task introspection
- Result retrieval via `get_result()`
- Event monitoring via Flower

## Setup

```bash
# 1. Start Valkey/Redis
docker compose up -d

# 2. Install dependencies (from repo root)
cd ../..
uv venv && uv sync --group dev
uv pip install Django flower
cd examples/django_tasks

# 3. Start the worker (with -E to enable events)
uv run celery -A proj worker --loglevel=info -E

# 4. In another terminal, send tasks
uv run python run.py

# 5. (Optional) Start Flower for event monitoring
uv run celery -A proj flower
```

## Expected output

```
============================================================
Django Tasks + celery-asyncio example
============================================================

1) Basic task: add(2, 3)
   result = 5

2) Async task: slow_add(10, 20) — async def with 1s sleep
   result = 30

3) Delayed task: add(10, 20) with run_after=+3s
   result = 30

4) ETA task: multiply(6, 7) with run_after=...
   result = 42

5) Priority tasks: three add() calls with different priorities
   low  (priority=-50): 2
   mid  (priority=0):   4
   high (priority=50):  6

6) High-priority task (priority=50 in @task decorator):
   result = urgent: hello

7) Task with context (takes_context=True):
   result = {'attempt': 1, 'task_id': '...', 'data': 'world'}

============================================================
All tasks completed successfully!
============================================================
```

## Web interface (optional)

The project also includes Django views for browser-based testing:

```bash
uv run python manage.py runserver
```

Open http://localhost:8000/ to enqueue tasks and check results via the browser.

## Flower dashboard

Open http://localhost:5555 in your browser while running tasks. You should see:

- **Workers tab** — the worker and its status
- **Tasks tab** — each task as it's sent, received, and completed
- **Monitor tab** — live graphs of task throughput and latency

## What's in the box

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Valkey/Redis container |
| `proj/settings.py` | Django + Celery + `TASKS` config |
| `proj/celery.py` | Celery app instance |
| `proj/tasks.py` | Five example tasks (`@task` decorator) |
| `proj/urls.py` | Web views for browser-based testing |
| `run.py` | CLI script to send tasks and print results |

## Configuration

The key setting is `TASKS` in `settings.py`:

```python
TASKS = {
    "default": {
        "BACKEND": "celery.contrib.django.CeleryBackend",
        "QUEUES": ["default"],
        "OPTIONS": {
            "celery_app": "proj.celery.app",
        },
    },
}
```

`CELERY_RESULT_EXTENDED = True` is required so that `get_result()`
can resolve the original task function from stored metadata.

## Cleanup

```bash
docker compose down
```
