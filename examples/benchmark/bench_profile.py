"""Sampling profiler for the benchmark workers, enabled with BENCH_PROFILE=1.

cProfile cannot see the other threads and distorts a run this tight, and the
out-of-process samplers do not know a free-threaded 3.14 yet. `sys._current_frames`
does, and sampling it from a daemon thread costs about one stack walk per OS
thread per tick.

Samples are written periodically rather than at exit, because a worker that is
terminated at the end of a run never gets to run its atexit hooks.

Read the package-level split, not the individual frames. Leaf attribution here
did not survive validation: it put 6.3% of on-CPU time in `_weakrefset._remove`,
a microbenchmark priced those callbacks at 0.47 us/task, and removing them
outright moved throughput by nothing. Use bench_counts.py and an A/B run to
attribute cost to a specific function.
"""

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

INTERVAL = float(os.environ.get("BENCH_PROFILE_INTERVAL", "0.005"))
OUT_DIR = Path(os.environ.get("BENCH_PROFILE_DIR", "results/profile"))

# Which package a frame belongs to, so the profile can say how much of the time
# is the task body and how much is the queue getting the task to it.
BUCKETS = (
    ("fw_common", "task body"),
    ("/celery/", "celery"),
    ("/kombu/", "kombu"),
    ("/taskiq", "taskiq"),
    ("/dramatiq/", "dramatiq"),
    ("/arq/", "arq"),
    ("/django_q/", "django-q2"),
    ("/redis/", "redis-py"),
    ("/asyncio/", "asyncio"),
)


def classify(filename: str) -> str:
    for needle, label in BUCKETS:
        if needle in filename:
            return label
    return "stdlib/other" if "/lib/python" in filename else "bench harness"


def frame_id(frame) -> str:
    code = frame.f_code
    return f"{classify(code.co_filename)}|{Path(code.co_filename).name}:{code.co_name}"


def sample(me: int) -> tuple[Counter, Counter, Counter]:
    leaves, buckets, stacks = Counter(), Counter(), Counter()
    for tid, frame in sys._current_frames().items():
        if tid == me:
            continue
        chain = []
        f = frame
        while f is not None:
            chain.append(frame_id(f))
            f = f.f_back
        if not chain:
            continue
        leaf = chain[0]
        leaves[leaf] += 1
        parked = leaf.split("|", 1)[1] in IDLE_LEAVES
        buckets["parked" if parked else leaf.split("|", 1)[0]] += 1
        if parked:
            continue
        stacks["<".join(chain[:12])] += 1
    return leaves, buckets, stacks


def run(label: str) -> None:
    me = threading.get_ident()
    leaves, buckets, stacks = Counter(), Counter(), Counter()
    out = OUT_DIR / f"{label}-{os.getpid()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    ticks = 0
    next_dump = time.monotonic() + 5.0
    while True:
        time.sleep(INTERVAL)
        a, b, c = sample(me)
        leaves += a
        buckets += b
        stacks += c
        ticks += 1
        now = time.monotonic()
        if now >= next_dump:
            next_dump = now + 5.0
            payload = {
                "label": label,
                "pid": os.getpid(),
                "ticks": ticks,
                "interval": INTERVAL,
                "buckets": dict(buckets.most_common()),
                "leaves": dict(leaves.most_common(80)),
                "stacks": dict(stacks.most_common(40)),
            }
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(out)


# Leaf frames that mean the thread is parked, not working. Sampling walks every
# thread whether or not it holds a core, so these are counted and set aside.
IDLE_LEAVES = frozenset(
    {
        "thread.py:_worker",
        "selectors.py:select",
        "threading.py:wait",
        "queue.py:get",
        "base_events.py:run_forever",
        "connection.py:wait",
        "socket.py:readinto",
        "runner_fw.py:main",
    },
)


def maybe_start(label: str) -> None:
    if os.environ.get("BENCH_PROFILE") != "1" or os.environ.get("BENCH_PROFILE_ROLE") != "worker":
        return
    threading.Thread(target=run, args=(label,), daemon=True).start()
    print(f"[bench_profile] sampling every {INTERVAL * 1000:.0f} ms", file=sys.stderr, flush=True)
