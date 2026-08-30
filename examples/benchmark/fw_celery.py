"""celery adapter, used for both the celery-asyncio and the classic celery row.

Driven by env so one module serves both:
    FW_CELERY_POOL=asyncio FW_PROCS=4 FW_CONC=25  -> 4 loop workers x 25 + 1 sync
    FW_CELERY_POOL=threads FW_CONC=100            -> upstream's thread pool
    FW_CELERY_PROCS=4                             -> that shape, N times over

Results are off here, unlike the main matrix, because every other framework in
this table also runs without a result backend. That is what makes the six rows
comparable, and it is why these two rows do not match the mixed table exactly.
"""

import os

import fw_common

from celery import Celery

IS_ASYNC = os.environ.get("FW_CELERY_ASYNC") == "1"

app = Celery("fwbench")
app.conf.update(
    broker_url=fw_common.REDIS_URL,
    task_ignore_result=True,
    worker_prefetch_multiplier=int(os.environ.get("FW_PREFETCH", "16")),
    task_default_queue="fwbench",
    task_routes={"fw.*": {"queue": "fwbench"}},
    broker_connection_retry_on_startup=True,
)


if os.environ.get("FW_STATE_PATCH") == "plainset":
    # A/B: the `requests` dict already holds a strong ref for the same lifetime.
    from celery.worker import state as _worker_state

    _worker_state.reserved_requests = set()
    _worker_state.active_requests = set()


@app.task(name="fw.sync")
def sync_task(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    return fw_common.work_sync(cpu_iters, io_seconds, mem_kb)


if IS_ASYNC:

    @app.task(name="fw.async")
    async def async_task(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
        return await fw_common.work_async(cpu_iters, io_seconds, mem_kb)


def worker_argv(bin_dir: str) -> list[str]:
    pool = os.environ.get("FW_CELERY_POOL", "threads")
    cmd = [
        f"{bin_dir}/celery",
        "-A",
        "fw_celery",
        "worker",
        "-Q",
        "fwbench",
        "--loglevel=warning",
        "--pool",
        pool,
    ]
    if pool == "asyncio":
        cmd += [
            "--loop-workers",
            os.environ.get("FW_PROCS", "4"),
            "--loop-concurrency",
            os.environ.get("FW_CONC", "25"),
            "--sync-workers",
            os.environ.get("FW_SYNC", "1"),
        ]
    else:
        cmd += ["--concurrency", os.environ.get("FW_CONC", "100")]
    procs = int(os.environ.get("FW_CELERY_PROCS", "1"))
    if procs == 1:
        return cmd + ["-n", "fwbench@%h"]
    # Separate OS processes, the shape arq and dramatiq use. Each needs its own
    # node name or celery warns about a duplicate and the second one exits.
    ones = [" ".join([*cmd, "-n", f"fwbench{i}@%h"]) for i in range(procs)]
    return ["sh", "-c", " & ".join(ones) + " & wait"]


def publish_loop(specs, backlog, done_fn, stop, state) -> None:
    task = app.tasks["fw.async" if IS_ASYNC else "fw.sync"]
    i = 0
    n = len(specs)
    while not stop.is_set():
        if i - done_fn() < backlog:
            for _ in range(200):
                task.apply_async(kwargs=specs[i % n], queue="fwbench")
                i += 1
            state["published"] = i
        else:
            stop.wait(0.02)
