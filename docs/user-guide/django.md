# Django Integration

## Django 6.0 Tasks

celery-asyncio ships a `CeleryBackend` that implements Django 6.0's `django.tasks` API (DEP 14). This lets you use Django's `@task` decorator and `enqueue()` API with Celery as the execution backend.

### Configuration

```python
# settings.py
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/1"
CELERY_RESULT_EXTENDED = True  # recommended for get_result()

TASKS = {
    "default": {
        "BACKEND": "celery.contrib.django.CeleryBackend",
        "QUEUES": ["default"],
    }
}
```

### Define tasks

```python
# myapp/tasks.py
from django.tasks import task

@task
def send_email(to, subject, body):
    ...

@task
async def process_payment(order_id):
    ...

@task(priority=10, queue_name="critical")
def urgent_task():
    ...
```

### Enqueue tasks

```python
# In views, management commands, etc.
from myapp.tasks import send_email, process_payment

result = send_email.enqueue(to="user@example.com", subject="Hi", body="Hello")
result = await process_payment.aenqueue(order_id=42)
```

### Get results

```python
result = send_email.get_result(result_id)
print(result.status)        # READY, RUNNING, SUCCESSFUL, FAILED
print(result.return_value)  # available when SUCCESSFUL
```

### Features

| Feature | Supported |
|---------|-----------|
| `supports_defer` | Yes, via `countdown`/`eta` |
| `supports_async_task` | Yes, native async |
| `supports_priority` | Yes, mapped to broker priority |
| `supports_get_result` | Yes, when result backend is configured |

### How it works

When `validate_task()` is called (at import time), the Django task function is registered as a Celery shared task using the same pattern as `@shared_task`. Both the web process (sender) and worker process import the same task modules, so the registration happens on both sides.

`enqueue()` calls `app.send_task()` to publish a message to the broker. The worker picks it up and executes the registered function.

## Classic Django + Celery

The traditional Django integration also works. Create a Celery app in your Django project:

```python
# myproject/celery.py
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

app = Celery("myproject")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# myproject/__init__.py
from .celery import app as celery_app
__all__ = ("celery_app",)
```
