# Django Integration

## Django 6.0 Tasks

For Django 6.0's `django.tasks` API (DEP 14) with celery-asyncio, use the [django-tasks-celery](https://github.com/oliverhaas/django-tasks-celery) package. It provides a backend that bridges Django's `@task` / `enqueue()` API with Celery's message-passing infrastructure.

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
