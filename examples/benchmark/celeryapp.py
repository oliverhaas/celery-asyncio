"""Celery app shared between celery-asyncio and classic celery venvs.

Both flavors import the same `app` and `tasks`. Broker/backend point at the
local valkey from docker-compose.yml.
"""

import os

from celery import Celery

BROKER_URL = os.environ.get("BENCH_BROKER", "redis://localhost:6379/0")
RESULT_URL = os.environ.get("BENCH_BACKEND", "redis://localhost:6379/1")

app = Celery("bench")

app.conf.update(
    broker_url=BROKER_URL,
    result_backend=RESULT_URL,
    task_acks_late=False,
    worker_prefetch_multiplier=int(os.environ.get("BENCH_PREFETCH", "16")),
    task_ignore_result=False,
    result_expires=600,
    broker_connection_retry_on_startup=True,
    task_default_queue="bench",
    task_routes={"bench.*": {"queue": "bench"}},
    include=["tasks"],
)
