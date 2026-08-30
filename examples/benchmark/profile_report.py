"""Summarise the sampling profiles written by bench_profile.py."""

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(label: str) -> tuple[Counter, Counter, int]:
    buckets, leaves = Counter(), Counter()
    for f in (ROOT / "results" / "profile").glob(f"{label}-*.json"):
        d = json.loads(f.read_text())
        buckets.update(d["buckets"])
        leaves.update(d["leaves"])
    return buckets, leaves, sum(buckets.values())


def report(label: str, top: int) -> None:
    buckets, leaves, total = load(label)
    if not total:
        print(f"{label}: no samples")
        return
    parked = buckets.pop("parked", 0)
    busy = total - parked
    run = ROOT / "results" / f"prof-{label}.json"
    print(f"\n=== {label} ===")
    if run.exists():
        d = json.loads(run.read_text())
        cpu = d["summary"]["mean_cpu_pct"]
        print(
            f"{d['throughput_tps']:.1f} tps at {cpu:.0f}% CPU, {cpu / 100 / d['throughput_tps'] * 1000:.3f} core-ms/task",
        )
    print(f"{total} samples, {parked / total * 100:.1f}% parked, {busy} on-CPU")
    print("\nshare of on-CPU samples by package:")
    for name, n in buckets.most_common():
        print(f"  {n / busy * 100:6.2f}%  {name}")
    print(f"\ntop {top} on-CPU frames:")
    idle = {
        "thread.py:_worker",
        "selectors.py:select",
        "threading.py:wait",
        "queue.py:get",
        "base_events.py:run_forever",
        "connection.py:wait",
        "socket.py:readinto",
        "runner_fw.py:main",
    }
    shown = 0
    for name, n in leaves.most_common():
        pkg, frame = name.split("|", 1)
        if frame in idle:
            continue
        print(f"  {n / busy * 100:6.2f}%  {pkg:12} {frame}")
        shown += 1
        if shown >= top:
            break


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="*", default=["celery-aio", "taskiq"])
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    for label in args.labels:
        report(label, args.top)
