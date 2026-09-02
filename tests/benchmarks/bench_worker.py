"""Task throughput benchmark.

Publishes tasks, runs a worker until it has consumed them, and reports how many
tasks a second each half managed. Needs a broker; RabbitMQ is the default::

    python tests/benchmarks/bench_worker.py both 20000
    BROKER=valkey://localhost:6379/15 python tests/benchmarks/bench_worker.py work

Results are ignored and messages are transient, so the numbers measure the
publish and consume paths rather than a result backend.
"""

import asyncio
import os
import sys
import time

from celery import Celery

DEFAULT_ITS = 40000
QUEUE = "bench.worker"

app = Celery("bench_worker")
app.conf.update(
    broker_url=os.environ.get("BROKER", "amqp://guest:guest@localhost:5672//"),
    result_backend=None,
    task_default_queue=QUEUE,
    task_default_delivery_mode=1,
    task_ignore_result=True,
    task_serializer="json",
    worker_prefetch_multiplier=64,
)


class counter:
    """Shared between the publisher, the tasks and the worker in one process."""

    seen = 0
    expected = 0
    subtotal = None
    started = None
    done = None


@app.task(queue=QUEUE, ignore_result=True)
def it(i, n):
    if not counter.seen:
        counter.started = counter.subtotal = time.monotonic()
    counter.seen += 1
    if not counter.seen % 5000:
        print(f"({counter.seen} so far: {time.monotonic() - counter.subtotal:.3f}s)", file=sys.stderr)
        counter.subtotal = time.monotonic()
    if counter.seen >= counter.expected:
        counter.done.set()


async def bench_apply(n=DEFAULT_ITS):
    task = it._get_current_object()
    started = time.monotonic()
    for i in range(n):
        await task.aapply_async((i, n))
    took = time.monotonic() - started
    print(f"-- apply {n} tasks: {took:.3f}s total, {n / took:.0f} tasks/s")


async def bench_work(n=DEFAULT_ITS, loglevel="CRITICAL"):
    loglevel = os.environ.get("BENCH_LOGLEVEL") or loglevel
    if loglevel:
        app.log.setup_logging_subsystem(loglevel=loglevel)

    counter.seen = 0
    counter.expected = n
    counter.done = asyncio.Event()

    worker = app.WorkController(concurrency=15, queues=[QUEUE], without_mingle=True, without_gossip=True)
    running = asyncio.ensure_future(worker.start())
    print("-- starting worker")
    try:
        await counter.done.wait()
    finally:
        await worker.stop()
        running.cancel()

    took = time.monotonic() - counter.started
    print(f"-- process {n} tasks: {took:.3f}s total, {n / took:.0f} tasks/s")


async def bench_both(n=DEFAULT_ITS):
    await bench_apply(n)
    await bench_work(n)


def main(argv=sys.argv):
    benchmarks = {"apply": bench_apply, "work": bench_work, "both": bench_both}
    if len(argv) < 2 or argv[1] not in benchmarks:
        print(f"Usage: {os.path.basename(argv[0])} [apply|work|both] [n={DEFAULT_ITS}]")
        return sys.exit(1)
    try:
        n = int(argv[2])
    except IndexError:
        n = DEFAULT_ITS
    return asyncio.run(benchmarks[argv[1]](n=n))


if __name__ == "__main__":
    main()
