"""dramatiq adapter: sync actors, Redis broker, processes x threads."""

import logging

import dramatiq
import fw_common
from dramatiq.brokers.redis import RedisBroker

logging.getLogger("dramatiq").setLevel(logging.WARNING)

broker = RedisBroker(url=fw_common.REDIS_URL)
# No Results middleware: dramatiq stores nothing by default and that is its
# fastest configuration, which is what this table asks for.
dramatiq.set_broker(broker)


@dramatiq.actor(max_retries=0, time_limit=600_000)
def bench_task(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    return fw_common.work_sync(cpu_iters, io_seconds, mem_kb)


def worker_argv(bin_dir: str) -> list[str]:
    import os

    procs = os.environ.get("FW_PROCS", "1")
    threads = os.environ.get("FW_THREADS", "100")
    return [f"{bin_dir}/dramatiq", "fw_dramatiq", "--processes", procs, "--threads", threads]


def publish_loop(specs, backlog, done_fn, stop, state) -> None:
    i = 0
    n = len(specs)
    while not stop.is_set():
        if i - done_fn() < backlog:
            for _ in range(200):
                bench_task.send(**specs[i % n])
                i += 1
            state["published"] = i
        else:
            stop.wait(0.02)
