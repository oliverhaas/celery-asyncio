"""arq adapter: asyncio-native, Redis, single process."""

import asyncio
import os

import fw_common
from arq import create_pool
from arq.connections import RedisSettings

CONC = int(os.environ.get("FW_CONC", "100"))


async def bench_task(ctx, cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    return await fw_common.work_async(cpu_iters, io_seconds, mem_kb)


class WorkerSettings:
    functions = [bench_task]
    max_jobs = CONC
    # Results are off in every framework here, so the table measures execution
    # rather than each project's result-backend defaults.
    keep_result = 0
    log_results = False
    # arq polls its queue rather than blocking on it, and the 0.5 s default
    # caps throughput long before the pool is busy.
    poll_delay = float(os.environ.get("FW_POLL", "0.01"))
    queue_read_limit = CONC * 5
    max_tries = 1
    job_timeout = 600
    redis_settings = RedisSettings.from_dsn(fw_common.REDIS_URL)


LOG_DICT = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"arq": {"handlers": ["null"], "level": "WARNING"}},
}


def worker_argv(bin_dir: str) -> list[str]:
    """arq has no multi-process flag: you scale it by running more processes."""
    procs = int(os.environ.get("FW_PROCS", "1"))
    if procs == 1:
        return [f"{bin_dir}/arq", "--custom-log-dict", "fw_arq.LOG_DICT", "fw_arq.WorkerSettings"]
    one = f"{bin_dir}/arq --custom-log-dict fw_arq.LOG_DICT fw_arq.WorkerSettings"
    return ["sh", "-c", " & ".join([one] * procs) + " & wait"]


def publish_loop(specs, backlog, done_fn, stop, state) -> None:
    async def main() -> None:
        pool = await create_pool(RedisSettings.from_dsn(fw_common.REDIS_URL))
        i = 0
        n = len(specs)
        while not stop.is_set():
            if i - done_fn() < backlog:
                await asyncio.gather(
                    *[pool.enqueue_job("bench_task", **specs[(i + k) % n]) for k in range(200)],
                )
                i += 200
                state["published"] = i
            else:
                await asyncio.sleep(0.02)

    asyncio.run(main())
