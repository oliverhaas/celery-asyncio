"""Celery app shared between celery-asyncio and classic celery venvs.

Both flavors import the same `app` and `tasks`. Broker/backend point at the
local valkey from docker-compose.yml.

Set BENCH_UVLOOP=1 in the environment to replace asyncio's default selector
event loop with uvloop (libuv-backed). This affects both the broker
drain_events loop and each LoopWorker's task-execution loop.
"""

import asyncio
import os
import sys

# Install uvloop BEFORE celery (which transitively imports asyncio) so the
# policy is in place when any loop is created. Verify by actually checking
# the policy after install — echo to stderr so each worker log records the
# exact event-loop class used (the line shows up in the worker logfile and
# makes uvloop activation observable per run, not just "we set the env var").
_UVLOOP_REQUESTED = os.environ.get("BENCH_UVLOOP") == "1"
if _UVLOOP_REQUESTED:
    import uvloop  # noqa: PLC0415

    uvloop.install()

_loop = asyncio.new_event_loop()
try:
    _LOOP_CLASS = f"{type(_loop).__module__}.{type(_loop).__qualname__}"
finally:
    _loop.close()
print(
    f"[celeryapp] BENCH_UVLOOP={'1' if _UVLOOP_REQUESTED else 'unset'}, event loop: {_LOOP_CLASS}",
    file=sys.stderr,
    flush=True,
)

from celery import Celery  # noqa: E402

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
