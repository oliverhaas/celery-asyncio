"""taskiq adapter: asyncio-native, Redis, no result backend.

FW_TASKIQ_BROKER picks the transport: `list` is taskiq's documented default
and what its quickstart shows, `stream` is the acknowledged one.
"""

import asyncio
import os

import fw_common
from taskiq_redis import ListQueueBroker, RedisStreamBroker

# LPUSH/BRPOP never acknowledges; the stream broker XACKs a consumer group.
if os.environ.get("FW_TASKIQ_BROKER", "list") == "stream":
    broker = RedisStreamBroker(url=fw_common.REDIS_URL)
else:
    broker = ListQueueBroker(url=fw_common.REDIS_URL)


@broker.task
async def bench_task(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    return await fw_common.work_async(cpu_iters, io_seconds, mem_kb)


def worker_argv(bin_dir: str) -> list[str]:
    procs = os.environ.get("FW_PROCS", "1")
    conc = os.environ.get("FW_CONC", "100")
    return [
        f"{bin_dir}/taskiq",
        "worker",
        "fw_taskiq:broker",
        "--workers",
        procs,
        "--max-async-tasks",
        conc,
        "--log-level",
        "WARNING",
        "--no-configure-logging",
    ]


def publish_loop(specs, backlog, done_fn, stop, state) -> None:
    async def main() -> None:
        await broker.startup()
        i = 0
        n = len(specs)
        while not stop.is_set():
            if i - done_fn() < backlog:
                await asyncio.gather(
                    *[bench_task.kiq(**specs[(i + k) % n]) for k in range(200)],
                )
                i += 200
                state["published"] = i
            else:
                await asyncio.sleep(0.02)

    asyncio.run(main())
