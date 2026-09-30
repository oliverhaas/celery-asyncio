"""Per-task cost of the framework itself, on one machine, in under a minute.

The matrix in run_all.py compares pools and frameworks across venvs on pinned
cores. This measures one thing: what a trivial task costs this checkout, in
worker CPU and in Redis commands, which is what an optimisation of the hot path
has to move. Everything runs against the Redis in QUICK_BROKER/QUICK_BACKEND,
whose databases it flushes.

    python quick_bench.py publish --mode async -n 3000
    python quick_bench.py worker --kind async -n 30000 [--events] [--loglevel info]
    python quick_bench.py worker --kind sync --sync-workers 4 -n 12000

`worker` fills the queue first, starts a worker, and measures the window
between the first and the last tenth of the results arriving, so neither
startup nor the tail is in it. CPU is read per thread from /proc (Linux only):
the consumer's main thread and each loop worker separately. On a loopback Redis
the kernel runs the server's receive path inside the client's send, so a round
trip weighs more here than across a network; compare runs with each other, not
with the tables in RESULTS.md.
"""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import redis

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from quick_app import app, noop_async, noop_sync

broker = redis.Redis.from_url(app.conf.broker_url)
backend = redis.Redis.from_url(app.conf.result_backend)


def commandstats():
    return {key.removeprefix("cmdstat_"): v["calls"] for key, v in broker.info("commandstats").items()}


def per_task(before, after, n):
    counts = {key: round((v - before.get(key, 0)) / n, 2) for key, v in after.items() if v != before.get(key, 0)}
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def thread_cpu(pid):
    """Ticks of CPU per thread of `pid`, by thread id, with the thread's name."""
    ticks = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            name = Path(f"/proc/{pid}/task/{tid}/comm").read_text().strip()
            fields = Path(f"/proc/{pid}/task/{tid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        ticks[tid] = (name, int(fields[11]) + int(fields[12]))
    return ticks


def publish(args):
    task = noop_async

    async def send(n):
        for i in range(n):
            await task.adelay(i)

    def run(n):
        if args.mode == "async":
            asyncio.run(send(n))
        else:
            for i in range(n):
                task.delay(i)

    broker.flushdb()
    run(50)
    broker.flushdb()
    broker.config_resetstat()
    before = commandstats()
    t0 = time.perf_counter()
    run(args.n)
    elapsed = time.perf_counter() - t0
    print(f"{args.mode} publish: {elapsed / args.n * 1e6:.1f} us each, {args.n / elapsed:.0f}/s")
    print("  redis commands per publish:", per_task(before, commandstats(), args.n))


async def fill(n, task):
    slots = asyncio.Semaphore(64)

    async def one(i):
        async with slots:
            await task.adelay(i)

    await asyncio.gather(*(one(i) for i in range(n)))


def worker(args):
    broker.flushdb()
    backend.flushdb()
    asyncio.run(fill(args.n, noop_async if args.kind == "async" else noop_sync))
    backend.flushdb()

    cmd = [
        str(Path(sys.executable).with_name("celery")),
        *("-A", "quick_app", "worker", "-P", "asyncio", "-l", args.loglevel),
        *("--loop-workers", str(args.loop_workers), "--loop-concurrency", str(args.loop_concurrency)),
        *("--sync-workers", str(args.sync_workers)),
        *("--without-mingle", "--without-gossip", "--without-heartbeat", "-n", f"quick{os.getpid()}@%h"),
    ]
    if args.events:
        cmd.append("-E")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")])))
    with (HERE / "quick_worker.log").open("w") as log:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=HERE, start_new_session=True)
        try:
            deadline = time.monotonic() + 300
            start, end = args.n // 10, args.n - args.n // 10

            def wait_for(count):
                while backend.dbsize() < count:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise SystemExit("the worker died or stalled, see quick_worker.log")
                    time.sleep(0.002)
                return time.perf_counter(), backend.dbsize(), thread_cpu(proc.pid), commandstats()

            t1, done1, cpu1, stats1 = wait_for(start)
            t2, done2, cpu2, stats2 = wait_for(end)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    n = done2 - done1
    tick = os.sysconf("SC_CLK_TCK")
    threads = [(name, (ticks - cpu1.get(tid, (name, 0))[1]) / tick) for tid, (name, ticks) in cpu2.items()]
    threads = sorted(((name, cpu) for name, cpu in threads if cpu > 0), key=lambda nc: -nc[1])
    total = sum(cpu for _, cpu in threads)
    print(
        f"{args.kind} tasks, events={'on' if args.events else 'off'}, log={args.loglevel}: "
        f"{n / (t2 - t1):.0f} tasks/s, {total / n * 1e6:.0f} us of worker CPU per task",
    )
    print("  per thread, us/task:", ", ".join(f"{name}={cpu / n * 1e6:.0f}" for name, cpu in threads))
    print("  redis commands per task:", per_task(stats1, stats2, n))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="what", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--mode", choices=("async", "sync"), default="async")
    pub.add_argument("-n", type=int, default=3000)
    work = sub.add_parser("worker")
    work.add_argument("--kind", choices=("async", "sync"), default="async")
    work.add_argument("-n", type=int, default=30000)
    work.add_argument("--events", action="store_true")
    work.add_argument("--loglevel", default="warning")
    work.add_argument("--loop-workers", type=int, default=1)
    work.add_argument("--loop-concurrency", type=int, default=100)
    work.add_argument("--sync-workers", type=int, default=1)
    args = parser.parse_args()
    (publish if args.what == "publish" else worker)(args)


if __name__ == "__main__":
    main()
