"""App for quick_bench.py: task bodies that do nothing, so the framework's own cost is all there is."""

import os

from celery import Celery

app = Celery(
    "quick_app",
    broker=os.environ["QUICK_BROKER"],
    backend=os.environ["QUICK_BACKEND"],
)
app.conf.update(
    worker_prefetch_multiplier=16,
    result_expires=600,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)


@app.task(name="quick.noop_async")
async def noop_async(x):
    return x


@app.task(name="quick.noop_sync")
def noop_sync(x):
    return x
