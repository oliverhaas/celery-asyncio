"""Run a few configs several times each, to see how much a single cell can be trusted.

The main matrix runs every cell once, which is fine for an order-of-magnitude
comparison and not fine for a claim like "A is 1.4x B". This re-runs the configs
a claim rests on and reports the spread:

    python repeat_runs.py --repeat 3 --profile mixed aio-async-l4c25-uvloop-314t

Results go to results/repeats/ so they never collide with the matrix output the
report is rendered from.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

import run_all

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "repeats"


def one(r: run_all.Run, workload: Path, profile: str, i: int) -> dict | None:
    venv = run_all.Venv.from_dir(r.venv)
    out = OUT / f"{r.label}-{profile.replace('-', '_')}-{i}.json"
    cmd = [
        str(venv.python),
        str(ROOT / "runner.py"),
        "--config",
        r.label,
        "--workload",
        str(workload),
        "--output",
        str(out),
        "--worker-bin",
        str(venv.celery_bin),
        "--variant",
        r.variant,
        "--run-timeout",
        "1800",
        "--ready-timeout",
        "120",
    ]
    for flag, value in (
        ("--pool", r.pool),
        ("--concurrency", r.concurrency),
        ("--loop-workers", r.loop_workers),
        ("--loop-concurrency", r.loop_concurrency),
        ("--sync-workers", r.sync_workers),
    ):
        if value is not None:
            cmd += [flag, str(value)]

    env = os.environ.copy()
    if r.env:
        env.update(r.env)
    run_all.flush_broker()
    print(f"[repeat] {r.label} run {i}", flush=True)
    t0 = time.monotonic()
    rc = subprocess.call(cmd, env=env, cwd=str(ROOT))
    print(f"[repeat] {r.label} run {i}: rc={rc} ({time.monotonic() - t0:.1f}s)", flush=True)
    return json.loads(out.read_text()) if rc == 0 and out.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="+")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--tasks", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--profile", default="mixed", choices=("mixed", "cpu-only", "io-only"))
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    workload = ROOT / "results" / f"workload-{args.profile.replace('-', '_')}-{args.tasks}-s{args.seed}.json"
    if not workload.exists():
        msg = f"{workload} missing; run run_all.py for this profile first"
        raise SystemExit(msg)

    by_label = {r.label: r for r in run_all.matrix()}
    runs = [by_label[label] for label in args.labels]

    collected: dict[str, list[dict]] = {}
    for i in range(1, args.repeat + 1):
        for r in runs:
            if (res := one(r, workload, args.profile, i)) is not None:
                collected.setdefault(r.label, []).append(res)

    print(f"\n{'config':<34} {'runs':>4} {'TPS mean':>9} {'min':>8} {'max':>8} {'spread':>7} {'PSS mean':>9}")
    print("-" * 88)
    for label, rows in collected.items():
        tps = [r["throughput_tps"] for r in rows]
        pss = [r["summary"]["peak_pss_mb"] for r in rows]
        spread = (max(tps) - min(tps)) / statistics.fmean(tps) if tps else 0
        print(
            f"{label:<34} {len(rows):>4} {statistics.fmean(tps):>9.1f} {min(tps):>8.1f} "
            f"{max(tps):>8.1f} {spread:>6.0%} {statistics.fmean(pss):>8.0f}M"
        )


if __name__ == "__main__":
    main()
