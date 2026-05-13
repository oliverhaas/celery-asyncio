"""Benchmark tasks: one workload profile, two flavors (sync, async).

Each task does a parametrized mix of:
  - CPU work: `cpu_iters` iterations of float arithmetic
  - I/O wait: `io_seconds` of sleep (time.sleep or asyncio.sleep)
  - Memory:   `mem_kb` of bytearray held for the task lifetime

`mem_kb` is in KiB to keep workload manifests compact. The bytearray is
filled with a non-zero pattern so a CoW-friendly allocator can't optimize
it away.
"""

import asyncio
import time

from celeryapp import app

# Stretches each iteration so cpu_iters values stay small in the workload JSON.
_CPU_INNER = 50


def _burn_cpu(cpu_iters: int) -> float:
    """Tight float loop. Returns the accumulator so the JIT/optimizer can't elide it."""
    acc = 0.0
    for i in range(cpu_iters):
        x = float(i)
        for _ in range(_CPU_INNER):
            x = x * 1.0000001 + 1.0
        acc += x
    return acc


def _alloc(mem_kb: int) -> bytearray:
    if mem_kb <= 0:
        return bytearray()
    buf = bytearray(mem_kb * 1024)
    # Touch every 4 KiB page so the pages are actually faulted in.
    for offset in range(0, len(buf), 4096):
        buf[offset] = 0xA5
    return buf


@app.task(name="bench.mixed_sync")
def mixed_sync(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    buf = _alloc(mem_kb)
    _burn_cpu(cpu_iters)
    if io_seconds > 0:
        time.sleep(io_seconds)
    return len(buf)


@app.task(name="bench.mixed_async")
async def mixed_async(cpu_iters: int, io_seconds: float, mem_kb: int) -> int:
    buf = _alloc(mem_kb)
    _burn_cpu(cpu_iters)
    if io_seconds > 0:
        await asyncio.sleep(io_seconds)
    return len(buf)
