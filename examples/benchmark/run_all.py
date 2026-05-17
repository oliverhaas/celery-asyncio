"""Drive the full benchmark matrix.

Iterates over (flavor, python, pool config) combinations, invokes runner.py
for each one in the matching venv, and writes one JSON file per run under
results/.

Run from this directory after `docker compose up -d` and `setup_venvs.sh`:

    python run_all.py --tasks 10000

Each cell is an isolated subprocess so a crash in one config doesn't poison
the rest.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class Venv:
    name: str
    python: Path
    celery_bin: Path

    @classmethod
    def from_dir(cls, name: str) -> "Venv":
        d = ROOT / name
        return cls(name=name, python=d / "bin" / "python", celery_bin=d / "bin" / "celery")


@dataclass
class Run:
    label: str
    venv: str
    pool: str | None
    concurrency: int | None
    loop_workers: int | None
    loop_concurrency: int | None
    sync_workers: int | None
    variant: str


def matrix() -> list[Run]:
    """Configurations to run.

    4 worker threads pinned to 4 cores (taskset 0,1,2,3) so free-threading
    has room to actually use the cores in parallel.

    celery-asyncio: 4 loop workers x 25 concurrency = 100 in-flight async slots.
    For the sync variant we route through 4 sync threads instead.
    For the mixed variant we split: 2 loop x 50 + 2 sync = 100 + 2 slots.
    Classic celery: prefork-4 (4 procs) or threads-4 (4 GIL-bound threads).
    """
    runs: list[Run] = []

    for venv in (".venv-async-314", ".venv-async-314t"):
        suffix = venv.split("-async-")[1]
        # async variant: 4 event loops (each on its own thread)
        runs.append(Run(f"aio-async-l4c25-{suffix}", venv, "asyncio", None, 4, 25, 1, "async"))
        # sync variant: 4 sync worker threads
        runs.append(Run(f"aio-sync-s4-{suffix}", venv, "asyncio", None, 1, 1, 4, "sync"))
        # mixed: 2 loops + 2 sync (4 worker threads total)
        runs.append(Run(f"aio-mixed-l2c50-s2-{suffix}", venv, "asyncio", None, 2, 50, 2, "mixed"))

    for venv in (".venv-classic-314", ".venv-classic-314t"):
        suffix = venv.split("-classic-")[1]
        # solo: 1 prefork proc -- baseline showing single-worker scaling and
        # memory floor comparable to the aio pool.
        runs.append(Run(f"classic-prefork1-{suffix}", venv, "prefork", 1, None, None, None, "sync"))
        runs.append(Run(f"classic-prefork4-{suffix}", venv, "prefork", 4, None, None, None, "sync"))
        runs.append(Run(f"classic-threads4-{suffix}", venv, "threads", 4, None, None, None, "sync"))

    return runs


def flush_broker() -> None:
    """Clear the valkey state between runs so leftover queues/results don't cross-contaminate."""
    subprocess.run(
        ["docker", "compose", "exec", "-T", "valkey", "valkey-cli", "FLUSHALL"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
    )


def run_one(r: Run, workload: Path, tasks: int, profile: str) -> tuple[bool, Path]:
    venv = Venv.from_dir(r.venv)
    profile_slug = profile.replace("-", "_")
    if not venv.celery_bin.exists():
        print(f"[skip] {r.label}: {venv.celery_bin} missing")
        return False, ROOT / "results" / f"{r.label}-{profile_slug}.json"

    out = ROOT / "results" / f"{r.label}-{profile_slug}.json"
    cmd = [
        str(venv.python),
        str(ROOT / "runner.py"),
        "--config", r.label,
        "--workload", str(workload),
        "--output", str(out),
        "--worker-bin", str(venv.celery_bin),
        "--variant", r.variant,
        "--run-timeout", "1800",
    ]
    if r.pool:
        cmd += ["--pool", r.pool]
    if r.concurrency is not None:
        cmd += ["--concurrency", str(r.concurrency)]
    if r.loop_workers is not None:
        cmd += ["--loop-workers", str(r.loop_workers)]
    if r.loop_concurrency is not None:
        cmd += ["--loop-concurrency", str(r.loop_concurrency)]
    if r.sync_workers is not None:
        cmd += ["--sync-workers", str(r.sync_workers)]

    env = os.environ.copy()
    env["BENCH_TASKS"] = str(tasks)

    print(f"\n[run_all] === {r.label} ===")
    print(f"[run_all] {' '.join(cmd)}")
    t0 = time.monotonic()
    rc = subprocess.call(cmd, env=env, cwd=str(ROOT))
    dt = time.monotonic() - t0
    print(f"[run_all] {r.label}: rc={rc} ({dt:.1f}s)")
    return rc == 0, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--only", nargs="*", help="run only configs whose label matches one of these substrings")
    ap.add_argument(
        "--profile",
        choices=("mixed", "cpu-only", "io-only"),
        default="mixed",
        help="workload profile: 'mixed' (default), 'cpu-only' (showcase GIL impact on threads), 'io-only' (showcase asyncio concurrency)",
    )
    ap.add_argument("--cpu-iters", type=int, default=None, help="override cpu_iters for cpu-only profile (default 20000 ≈ 5 ms)")
    ap.add_argument("--io-seconds", type=float, default=None, help="override io_seconds for io-only profile (default 0.1 s)")
    args = ap.parse_args()

    # Generate workload (one for the entire matrix).
    profile_slug = args.profile.replace("-", "_")
    workload = ROOT / "results" / f"workload-{profile_slug}-{args.tasks}-s{args.seed}.json"
    workload.parent.mkdir(parents=True, exist_ok=True)
    if not workload.exists():
        cmd = [
            sys.executable, str(ROOT / "workload.py"),
            "--count", str(args.tasks),
            "--seed", str(args.seed),
            "--profile", args.profile,
            "--out", str(workload),
        ]
        if args.cpu_iters is not None:
            cmd += ["--cpu-iters", str(args.cpu_iters)]
        if args.io_seconds is not None:
            cmd += ["--io-seconds", str(args.io_seconds)]
        subprocess.check_call(cmd)

    runs = matrix()
    if args.only:
        runs = [r for r in runs if any(sub in r.label for sub in args.only)]

    print(f"[run_all] running {len(runs)} configs with {args.tasks} tasks each")
    results: list[dict] = []
    for r in runs:
        flush_broker()
        ok, out = run_one(r, workload, args.tasks, args.profile)
        if ok and out.exists():
            results.append(json.loads(out.read_text()))

    # Print summary table.
    print("\n" + "=" * 110)
    print(f"SUMMARY ({args.profile} profile, {args.tasks} tasks)")
    print("=" * 110)
    print(
        f"{'config':<32} {'variant':<8} {'time':>8} {'tps':>8} {'peak_rss':>10} {'mean_cpu':>10} {'stranded':>10}"
    )
    print("-" * 110)
    for r in sorted(results, key=lambda x: x["complete_seconds"]):
        s = r["summary"]
        print(
            f"{r['config']:<32} {r['variant']:<8} "
            f"{r['complete_seconds']:>7.1f}s {r['throughput_tps']:>8.1f} "
            f"{s['peak_rss_mb']:>8.1f}M {s['mean_cpu_pct']:>9.1f}% "
            f"{r['n_stranded']:>10}",
        )


if __name__ == "__main__":
    main()
