"""Task body and completion counter shared by every framework adapter.

The work is byte-for-byte the same as `tasks.py` so a cross-framework table
compares schedulers rather than workloads. What differs from `tasks.py` is how
completions are counted: celery writes one result key per task and the celery
runner counts those with DBSIZE, but dramatiq stores no results by default and
django-q2 stores them in an ORM, so there is no shared key layout to count.

Every task therefore bumps one Redis counter itself. That is one extra
round-trip per task, identical in all six configurations, so it shifts every
row by the same amount rather than biasing the comparison.
"""

import asyncio
import os
import sys
import threading
import time

_TIME_BODY = os.environ.get("BENCH_TIME_BODY") == "1"
_body_ns = [0]
_body_n = [0]
# Free threading gives no atomic +=, and lost updates would bias the arms
# differently, so the two counters take a lock.
_body_lock = threading.Lock()


COUNTER = "bench:done"
EXTRA_KEY = "bench:extra"
# Prices one Redis round-trip by adding one and reading off the slope.
EXTRA_ROUNDTRIPS = int(os.environ.get("BENCH_EXTRA_ROUNDTRIPS", "0"))
# Only used to flush the broker db between runs, always through redis-py, so a
# valkey:// broker has to come back to a scheme redis-py will parse.
REDIS_URL = os.environ.get("BENCH_BROKER", "redis://localhost:6379/0").replace("valkey://", "redis://", 1)
COUNTER_URL = os.environ.get("BENCH_COUNTER", "redis://localhost:6379/2")

# arq pulls redis[hiredis], which re-enables the GIL on a free-threading build.
print(
    f"[fw_common] gil_enabled: {getattr(sys, '_is_gil_enabled', lambda: True)()}",
    file=sys.stderr,
    flush=True,
)

import bench_counts
import bench_dprof
import bench_profile

_label = os.environ.get("BENCH_PROFILE_LABEL", "worker")
bench_profile.maybe_start(_label)
bench_counts.maybe_start(_label)
bench_dprof.maybe_start(_label)

if _TIME_BODY and os.environ.get("BENCH_PROFILE_ROLE") == "worker":

    def report_body() -> None:
        while True:
            time.sleep(5.0)
            with _body_lock:
                ns, n = _body_ns[0], _body_n[0]
            if n:
                print(f"[body] {ns / n / 1000:.1f} core-us/task over {n} tasks", file=sys.stderr, flush=True)

    threading.Thread(target=report_body, daemon=True).start()

_CPU_INNER = 50


def burn_cpu(cpu_iters: int) -> float:
    """Tight float loop. Returns the accumulator so the optimizer can't elide it."""
    acc = 0.0
    for i in range(cpu_iters):
        x = float(i)
        for _ in range(_CPU_INNER):
            x = x * 1.0000001 + 1.0
        acc += x
    return acc


def alloc(mem_kb: int) -> bytearray:
    if mem_kb <= 0:
        return bytearray()
    buf = bytearray(mem_kb * 1024)
    # Touch every 4 KiB page so the pages are actually faulted in.
    for offset in range(0, len(buf), 4096):
        buf[offset] = 0xA5
    return buf


_sync_client = None


def counter_sync():
    global _sync_client
    if _sync_client is None:
        import redis

        _sync_client = redis.Redis.from_url(COUNTER_URL)
    return _sync_client


# One client per loop: a shared one serialises every LoopWorker thread.
_async_clients: dict[int, object] = {}


def counter_async():
    import redis.asyncio

    key = id(asyncio.get_running_loop())
    client = _async_clients.get(key)
    if client is None:
        client = redis.asyncio.Redis.from_url(COUNTER_URL)
        _async_clients[key] = client
    return client


def work_sync(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    buf = alloc(mem_kb)
    burn_cpu(cpu_iters)
    if io_seconds > 0:
        time.sleep(io_seconds)
    counter_sync().incr(COUNTER)
    return len(buf)


async def work_async(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    # Identical code across frameworks, so a difference here is the host.
    if _TIME_BODY:
        t0 = time.thread_time_ns()
        buf = alloc(mem_kb)
        burn_cpu(cpu_iters)
        elapsed = time.thread_time_ns() - t0
        with _body_lock:
            _body_ns[0] += elapsed
            _body_n[0] += 1
    else:
        buf = alloc(mem_kb)
        burn_cpu(cpu_iters)
    if io_seconds > 0:
        await asyncio.sleep(io_seconds)
    await counter_async().incr(COUNTER)
    for _ in range(EXTRA_ROUNDTRIPS):
        await counter_async().incr(EXTRA_KEY)
    return len(buf)
