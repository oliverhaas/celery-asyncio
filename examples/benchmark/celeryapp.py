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
    import uvloop

    uvloop.install()

_loop = asyncio.new_event_loop()
try:
    _LOOP_CLASS = f"{type(_loop).__module__}.{type(_loop).__qualname__}"
finally:
    _loop.close()
# A free-threading build re-enables the GIL at runtime if it imports an
# extension that has not declared support, which turns a 4-core run into a
# 1-core one with nothing in the logs to say so. Report it per process.
_GIL = getattr(sys, "_is_gil_enabled", lambda: True)()
print(
    f"[celeryapp] BENCH_UVLOOP={'1' if _UVLOOP_REQUESTED else 'unset'}, event loop: {_LOOP_CLASS}, gil_enabled: {_GIL}",
    file=sys.stderr,
    flush=True,
)

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

# How long the consumer blocks server-side per iteration. Exposed because a
# worker that becomes ready just before the driver publishes can sit out its
# whole first block doing nothing, which lands in the measured wall-clock.
if (_block := os.environ.get("BENCH_BLOCK_TIMEOUT")) is not None:
    app.conf.broker_transport_options = {"block_timeout": float(_block)}

# Again after celery, kombu and the redis driver are imported: any one of them
# can be the extension that flips the GIL back on, and the reading above is
# taken too early to see it.
print(
    f"[celeryapp] post-import gil_enabled: {getattr(sys, '_is_gil_enabled', lambda: True)()}",
    file=sys.stderr,
    flush=True,
)
