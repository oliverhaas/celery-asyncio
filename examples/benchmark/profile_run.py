"""One-off profiling harness: run worker under py-spy + publish a workload.

Usage:
    python profile_run.py --workload results/workload-cpu_only-1000-s42.json \
        --venv .venv-async-314t --label 314t-cpu --duration 20

Produces:
    profiles/<label>.svg   (flamegraph, --threads, --idle)
    profiles/<label>.speedscope.json (for speedscope.app)
    profiles/<label>.log   (worker stderr/stdout)
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", required=True, type=Path)
    ap.add_argument("--venv", required=True, help="venv dir name relative to this script")
    ap.add_argument("--label", required=True, help="output filename stem")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--rate", type=int, default=200)
    ap.add_argument("--loop-workers", type=int, default=4)
    ap.add_argument("--loop-concurrency", type=int, default=25)
    ap.add_argument("--sync-workers", type=int, default=1)
    ap.add_argument("--taskset", default="0,1,2,3")
    ap.add_argument("--format", choices=("flamegraph", "speedscope", "both"), default="both")
    args = ap.parse_args()

    venv = HERE / args.venv
    py_spy = venv / "bin" / "py-spy"
    celery_bin = venv / "bin" / "celery"
    profiles = HERE / "profiles"
    profiles.mkdir(exist_ok=True)
    log_path = profiles / f"{args.label}.worker.log"

    worker_cmd = (
        ["taskset", "-c", args.taskset, str(celery_bin), "-A", "celeryapp", "worker"]
        + ["-Q", "bench", "--loglevel=info", "--logfile", str(log_path)]
        + ["-n", f"profile-{os.getpid()}@%h"]
        + ["--pool", "asyncio"]
        + ["--loop-workers", str(args.loop_workers)]
        + ["--loop-concurrency", str(args.loop_concurrency)]
        + ["--sync-workers", str(args.sync_workers)]
    )

    # Wrap the worker in py-spy record. --idle catches off-CPU time too.
    fmt = args.format
    runs: list[tuple[str, list[str]]] = []
    if fmt in ("flamegraph", "both"):
        out = profiles / f"{args.label}.svg"
        runs.append(("flamegraph", [
            str(py_spy), "record", "-o", str(out),
            "--duration", str(args.duration),
            "--rate", str(args.rate),
            "--threads", "--idle",
            "--subprocesses",
            "--",
        ] + worker_cmd))
    if fmt in ("speedscope", "both"):
        out = profiles / f"{args.label}.speedscope.json"
        runs.append(("speedscope", [
            str(py_spy), "record", "-o", str(out),
            "-f", "speedscope",
            "--duration", str(args.duration),
            "--rate", str(args.rate),
            "--threads", "--idle",
            "--subprocesses",
            "--",
        ] + worker_cmd))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"

    for kind, cmd in runs:
        # Each profile run = flush + start worker under py-spy + publish + wait.
        subprocess.run(["redis-cli", "-h", "localhost", "-p", "6379", "FLUSHALL"],
                       check=False, capture_output=True)
        if log_path.exists():
            log_path.unlink()

        print(f"[profile] starting {kind}: {' '.join(cmd[:8])} ...", flush=True)
        proc = subprocess.Popen(cmd, env=env, cwd=str(HERE),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for the worker to be ready by tailing the log.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if log_path.exists() and " ready." in log_path.read_text(errors="replace"):
                break
            time.sleep(0.2)
        else:
            print("[profile] worker did not become ready in 30s", flush=True)
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)
            sys.exit(1)

        # Publish the workload from the same venv (matches celery-asyncio).
        publish = [
            str(venv / "bin" / "python"), "-c",
            "import json, sys, time\n"
            "sys.path.insert(0, '.')\n"
            "from celeryapp import app\n"
            "from tasks import mixed_async\n"
            f"doc = json.load(open({str(args.workload)!r}))\n"
            "results = [mixed_async.apply_async(kwargs=t['kwargs'], queue='bench') for t in doc['tasks']]\n"
            "n = len(results); t0 = time.monotonic()\n"
            "deadline = t0 + 60\n"
            "while time.monotonic() < deadline:\n"
            "    done = sum(1 for r in results if r.ready())\n"
            "    if done >= n: break\n"
            "    time.sleep(0.2)\n"
            "print(f'published {n} in {time.monotonic() - t0:.1f}s, done={done}/{n}', flush=True)\n"
        ]
        subprocess.run(publish, env=env, cwd=str(HERE), check=False)

        # Let py-spy finish (it will exit on its own when --duration elapses;
        # then it sends SIGINT to the wrapped worker).
        try:
            proc.wait(timeout=args.duration + 30)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=10)

        print(f"[profile] {kind} done -> profiles/{args.label}.{('svg' if kind == 'flamegraph' else 'speedscope.json')}", flush=True)


if __name__ == "__main__":
    main()
