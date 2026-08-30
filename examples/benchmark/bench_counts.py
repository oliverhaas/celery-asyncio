"""Exact per-task Python call counts, enabled with BENCH_COUNTS=1.

The sampling profiler in bench_profile.py could not be trusted on this workload:
it blamed frames that an A/B run showed cost nothing. Counts cannot drift that
way. They say nothing about time, but "celery calls this 40 times per task" is
a structural fact, and multiplying it by a microbenchmark gives a cost model
that can be checked against a real A/B.

Everything is normalised by the number of task bodies entered, which is counted
by the same mechanism, so warmup and run length drop out.
"""

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

OUT_DIR = Path(os.environ.get("BENCH_COUNTS_DIR", "results/counts"))
BODY = ("work_async", "work_sync")

_local = threading.local()
_counters: list[Counter] = []
_add_lock = threading.Lock()


def on_call(code, offset) -> None:
    counter = getattr(_local, "counter", None)
    if counter is None:
        counter = _local.counter = Counter()
        with _add_lock:
            _counters.append(counter)
    counter[f"{Path(code.co_filename).parent.name}/{Path(code.co_filename).name}:{code.co_name}"] += 1


def merged() -> Counter:
    total = Counter()
    for counter in list(_counters):
        while True:
            try:
                total.update(dict(counter))
            except RuntimeError:
                continue
            break
    return total


def dump(label: str) -> None:
    out = OUT_DIR / f"{label}-{os.getpid()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        time.sleep(5.0)
        total = merged()
        tasks = sum(n for name, n in total.items() if name.split(":")[-1] in BODY)
        if not tasks:
            continue
        payload = {
            "label": label,
            "pid": os.getpid(),
            "tasks": tasks,
            "calls_per_task": round(sum(total.values()) / tasks, 1),
            "top": {name: round(n / tasks, 3) for name, n in total.most_common(120)},
        }
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(out)


def maybe_start(label: str) -> None:
    if os.environ.get("BENCH_COUNTS") != "1" or os.environ.get("BENCH_PROFILE_ROLE") != "worker":
        return
    tool = sys.monitoring.PROFILER_ID
    sys.monitoring.use_tool_id(tool, "bench_counts")
    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, on_call)
    sys.monitoring.set_events(tool, sys.monitoring.events.PY_START)
    threading.Thread(target=dump, args=(label,), daemon=True).start()
    print("[bench_counts] counting PY_START", file=sys.stderr, flush=True)
