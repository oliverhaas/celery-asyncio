# Django 6.0 Tasks + Celery Example

Demonstrates using **celery-asyncio** as the backend for Django 6.0's
built-in `django.tasks` framework (DEP 14).

Tasks are defined with Django's `@task` decorator — no Celery imports
needed in application code.  Under the hood, `CeleryBackend` dispatches
work via Celery's message-passing infrastructure.

## Prerequisites

- Python 3.14+
- Redis running on `localhost:6379`
- `celery-asyncio` and `Django>=6.0` installed

## Quick start

All commands are run from **this directory** (`examples/django_tasks/`).

### 1. Start a Celery worker

```bash
celery -A proj worker -l info
```

### 2. Start the Django dev server (separate terminal)

```bash
python manage.py runserver
```

### 3. Enqueue tasks

Open http://localhost:8000/ and click any of the links, or use curl:

```bash
# Enqueue a simple add task
curl 'http://localhost:8000/enqueue/add/?x=2&y=3'
# → {"task_id": "abc-123", "status": "Ready"}

# Check the result
curl 'http://localhost:8000/result/abc-123/'
# → {"task_id": "abc-123", "status": "Successful", "return_value": 5}
```

## What's in the box

| File | Purpose |
|------|---------|
| `proj/settings.py` | `TASKS` config pointing to `CeleryBackend` |
| `proj/celery.py` | Celery app instance |
| `proj/tasks.py` | Four example tasks (`@task` decorator) |
| `proj/urls.py` | Views that enqueue tasks and retrieve results |

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

`CELERY_RESULT_EXTENDED = True` is recommended so that `get_result()`
can resolve the original task function from stored metadata.

## Example tasks

```python
from django.tasks import task

@task
def add(x, y):
    return x + y

@task
async def slow_add(x, y):
    await asyncio.sleep(1)
    return x + y

@task(priority=50)
def high_priority_task(message):
    return f"urgent: {message}"

@task(takes_context=True)
def task_with_context(context, data):
    return {"attempt": context.attempt, "data": data}
```
