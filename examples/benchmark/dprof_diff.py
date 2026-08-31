"""Bucket two dprof dumps the same way, so the frameworks can be compared.

Buckets by the package a function lives in, since the dprof key carries the
parent directory. The task body is identical across frameworks by construction,
so it is the control: if it does not match, the runs are not comparable.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

BODY_FILES = ("fw_common", "bench_")
CLIENT = ("redis", "valkey", "_parsers", "asyncio")  # asyncio dir inside redis-py
STDLIB = ("asyncio", "python3.14t", "concurrent", "selectors")
FRAMEWORKS = ("celery", "kombu", "taskiq", "taskiq_redis", "taskiq_dependencies")


def bucket(fn: str) -> str:
    parent, _, rest = fn.partition("/")
    name = rest.split(":")[0]
    if any(b in name for b in BODY_FILES):
        return "task body + bench"
    if parent in ("redis", "valkey") or (name.startswith(("connection", "client")) and parent in CLIENT):
        return "redis client"
    if parent in FRAMEWORKS:
        return f"framework ({parent})"
    if parent in STDLIB or parent.startswith("python3"):
        return "stdlib asyncio"
    return f"other ({parent})"


def load(path: Path) -> tuple[dict, float, int]:
    dumps = sorted(path.glob("*.json"))
    agg: defaultdict = defaultdict(float)
    calls: defaultdict = defaultdict(float)
    total = 0.0
    tasks = 0
    for d in dumps:
        data = json.loads(d.read_text())
        tasks += data["tasks"]
        for row in data["rows"]:
            b = bucket(row["fn"])
            agg[b] += row["adj_us_per_task"] * data["tasks"]
            calls[b] += row["calls_per_task"] * data["tasks"]
            total += row["adj_us_per_task"] * data["tasks"]
    return {k: v / tasks for k, v in agg.items()}, total / tasks, sum(calls.values()) / tasks


a_name, b_name = sys.argv[1], sys.argv[2]
a, a_tot, a_calls = load(Path(f"results/dprof-{a_name}"))
b, b_tot, b_calls = load(Path(f"results/dprof-{b_name}"))

print(f"{'bucket':28} {a_name:>12} {b_name:>12} {'delta':>10}")
print("-" * 66)
for k in sorted(set(a) | set(b), key=lambda k: -(a.get(k, 0) + b.get(k, 0))):
    av, bv = a.get(k, 0.0), b.get(k, 0.0)
    print(f"{k:28} {av:10.1f}us {bv:10.1f}us {av - bv:+9.1f}us")
print("-" * 66)
print(f"{'TOTAL (profiled, inflated)':28} {a_tot:10.1f}us {b_tot:10.1f}us {a_tot - b_tot:+9.1f}us")
print(f"{'python calls/task':28} {a_calls:10.0f}  {b_calls:12.0f}  {a_calls - b_calls:+9.0f}")
