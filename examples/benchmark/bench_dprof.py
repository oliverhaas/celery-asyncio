"""Deterministic per-function timing via sys.monitoring, with BENCH_DPROF=1.

Sampling was not trustworthy here, so this measures every call instead. Coroutine
resume and yield are treated as their own push and pop, which is what makes the
timings mean anything on an event loop: a coroutine awaiting a socket is not
charged for the wait.

Every call pays two Python callbacks, so absolute times are inflated. The
inflation is proportional to call count, so `adjusted` subtracts a calibrated
per-event cost and that is the column to rank by.
"""

import json
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

OUT_DIR = Path(os.environ.get("BENCH_DPROF_DIR", "results/dprof"))
BODY = ("work_async", "work_sync")
_local = threading.local()
_threads: list = []
_add_lock = threading.Lock()
_clock = time.perf_counter_ns


class ThreadStats:
    __slots__ = ("calls", "self_ns", "stack")

    def __init__(self) -> None:
        self.stack: list[list] = []
        self.self_ns: defaultdict = defaultdict(int)
        self.calls: defaultdict = defaultdict(int)


def stats() -> ThreadStats:
    st = getattr(_local, "st", None)
    if st is None:
        st = _local.st = ThreadStats()
        with _add_lock:
            _threads.append(st)
    return st


def push(code, offset) -> None:
    st = stats()
    st.calls[code] += 1
    st.stack.append([code, _clock(), 0])


def resume(code, offset) -> None:
    stats().stack.append([code, _clock(), 0])


def pop(code, *rest) -> None:
    st = stats()
    stack = st.stack
    while stack and stack[-1][0] is not code:
        stack.pop()
    if not stack:
        return
    entry = stack.pop()
    elapsed = _clock() - entry[1]
    st.self_ns[code] += elapsed - entry[2]
    if stack:
        stack[-1][2] += elapsed


def key(code) -> str:
    return f"{Path(code.co_filename).parent.name}/{Path(code.co_filename).name}:{code.co_name}"


def merge() -> tuple[dict, dict]:
    self_ns: defaultdict = defaultdict(int)
    calls: defaultdict = defaultdict(int)
    for st in list(_threads):
        for code, ns in list(st.self_ns.items()):
            self_ns[key(code)] += ns
        for code, n in list(st.calls.items()):
            calls[key(code)] += n
    return self_ns, calls


def calibrate() -> float:
    """Nanoseconds of callback cost per monitored event, measured in place."""

    def noop() -> None:
        pass

    n = 20000
    t0 = _clock()
    for _ in range(n):
        noop()
    return max((_clock() - t0) / n, 0.0)


def dump(label: str, overhead_ns: float) -> None:
    out = OUT_DIR / f"{label}-{os.getpid()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        time.sleep(5.0)
        self_ns, calls = merge()
        tasks = sum(n for name, n in calls.items() if name.split(":")[-1] in BODY)
        if not tasks:
            continue
        rows = []
        for name, ns in self_ns.items():
            adjusted = max(ns - calls[name] * overhead_ns, 0.0)
            rows.append((adjusted, ns, calls[name], name))
        rows.sort(reverse=True)
        out.with_suffix(".tmp").write_text(
            json.dumps(
                {
                    "label": label,
                    "pid": os.getpid(),
                    "tasks": tasks,
                    "overhead_ns_per_event": round(overhead_ns, 1),
                    "rows": [
                        {
                            "fn": name,
                            "adj_us_per_task": round(adj / 1000 / tasks, 3),
                            "raw_us_per_task": round(raw / 1000 / tasks, 3),
                            "calls_per_task": round(n / tasks, 2),
                        }
                        for adj, raw, n, name in rows[:90]
                    ],
                },
                indent=2,
            ),
        )
        out.with_suffix(".tmp").replace(out)


def maybe_start(label: str) -> None:
    if os.environ.get("BENCH_DPROF") != "1" or os.environ.get("BENCH_PROFILE_ROLE") != "worker":
        return
    events = sys.monitoring.events
    tool = sys.monitoring.PROFILER_ID
    sys.monitoring.use_tool_id(tool, "bench_dprof")
    sys.monitoring.register_callback(tool, events.PY_START, push)
    sys.monitoring.register_callback(tool, events.PY_RESUME, resume)
    sys.monitoring.register_callback(tool, events.PY_RETURN, pop)
    sys.monitoring.register_callback(tool, events.PY_YIELD, pop)
    sys.monitoring.register_callback(tool, events.PY_UNWIND, pop)
    sys.monitoring.set_events(
        tool,
        events.PY_START | events.PY_RESUME | events.PY_RETURN | events.PY_YIELD | events.PY_UNWIND,
    )
    overhead = calibrate()
    threading.Thread(target=dump, args=(label, overhead), daemon=True).start()
    print(f"[bench_dprof] {overhead:.0f} ns per monitored call", file=sys.stderr, flush=True)
