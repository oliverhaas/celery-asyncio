"""The same task bodies with no framework at all, in the same pool shape.

Every cross-framework row spends its CPU on two things: the task body, which is
identical everywhere, and the queue's own per-task work. This runs the bodies
alone so the first term is measured rather than assumed, which is what turns a
throughput ranking into a statement about overhead.
"""

import argparse
import asyncio
import json
import threading
import time
from itertools import count
from pathlib import Path

import fw_common
import psutil


def run_sync(specs: list[dict], threads: int, stop: threading.Event, done: count) -> list[threading.Thread]:
    def worker() -> None:
        n = len(specs)
        for i in count():
            if stop.is_set():
                return
            fw_common.work_sync(**specs[i % n])
            next(done)

    pool = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in pool:
        t.start()
    return pool


def run_async(specs: list[dict], loops: int, conc: int, stop: threading.Event, done: count) -> list[threading.Thread]:
    async def slot(offset: int, step: int) -> None:
        n = len(specs)
        for i in count(offset, step):
            if stop.is_set():
                return
            await fw_common.work_async(**specs[i % n])
            next(done)

    def loop_thread(k: int) -> None:
        async def main() -> None:
            await asyncio.gather(*[slot(k * conc + j, loops * conc) for j in range(conc)])

        asyncio.run(main())

    pool = [threading.Thread(target=loop_thread, args=(k,), daemon=True) for k in range(loops)]
    for t in pool:
        t.start()
    return pool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", type=Path, required=True)
    ap.add_argument("--mode", choices=("sync", "async"), default="async")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--loops", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=60.0)
    args = ap.parse_args()

    specs = [t["kwargs"] for t in json.loads(args.workload.read_text())["tasks"]]
    done = count()
    stop = threading.Event()
    if args.mode == "sync":
        run_sync(specs, args.threads, stop, done)
        shape = f"{args.threads} sync threads"
    else:
        run_async(specs, args.loops, args.concurrency, stop, done)
        shape = f"{args.loops} loops x {args.concurrency}"

    proc = psutil.Process()
    time.sleep(args.warmup)
    c0, t0 = proc.cpu_times(), time.monotonic()
    n0 = next(done)
    time.sleep(args.duration)
    c1, t1 = proc.cpu_times(), time.monotonic()
    n1 = next(done)
    stop.set()

    window = t1 - t0
    completed = n1 - n0
    cpu_s = (c1.user - c0.user) + (c1.system - c0.system)
    tps = completed / window
    print(
        json.dumps(
            {
                "mode": args.mode,
                "shape": shape,
                "tps": round(tps, 1),
                "mean_cpu_pct": round(cpu_s / window * 100, 1),
                "core_ms_per_task": round(cpu_s / completed * 1000, 3),
                "completed": completed,
            },
        ),
    )


if __name__ == "__main__":
    main()
