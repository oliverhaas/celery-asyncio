"""Celery app configured with celery-asyncio."""

from celery import Celery

app = Celery("example")

app.config_from_object(
    {
        "broker_url": "redis://localhost:6379/0",
        "result_backend": "redis://localhost:6379/1",
        "include": ["tasks"],
    },
)
