"""Run one non-celery framework configuration on the same measurement core.

Same window, same sampler, same core pinning as `runner.py`, so rows produced
here sit next to celery rows honestly. The framework-specific parts are behind
a tiny adapter protocol that each `fw_*.py` module implements:

    worker_argv(bin_dir) -> list[str]
    publish_loop(specs, backlog, done_fn, stop, state) -> None

Readiness is uniform and needs no log parsing: the publisher starts immediately
and the worker counts as ready the moment the shared completion counter moves.
"""

import argparse
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import redis
from bench_core import HARNESS, sample_loop, summarize

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--framework", required=True, help="module suffix, e.g. arq for fw_arq.py")
    ap.add_argument("--config", required=True)
    ap.add_argument("--workload", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--slots", type=int, required=True, help="declared execution slots, for the table")
    ap.add_argument("--python-label", default="")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--warmup", type=float, default=10.0)
    ap.add_argument("--backlog", type=int, default=4000)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--taskset", default="0,1,2,3")
    ap.add_argument("--ready-timeout", type=float, default=120.0)
    args = ap.parse_args()

    mod = importlib.import_module(f"fw_{args.framework}")
    specs = [t["kwargs"] for t in json.loads(args.workload.read_text())["tasks"]]

    import fw_common

    counter = redis.Redis.from_url(fw_common.COUNTER_URL)
    counter.delete(fw_common.COUNTER)
    redis.Redis.from_url(fw_common.REDIS_URL).flushdb()

    def done_fn() -> int:
        return int(counter.get(fw_common.COUNTER) or 0)

    bin_dir = str(Path(sys.executable).parent)
    cmd: list[str] = []
    if args.taskset and shutil.which("taskset"):
        cmd += ["taskset", "-c", args.taskset]
    cmd += mod.worker_argv(bin_dir)

    log_path = ROOT / "results" / f"{args.config}.worker.log"
    log_path.parent.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)

    stop_sample = threading.Event()
    stop_pub = threading.Event()
    samples: list[dict] = []
    state = {"published": 0}

    with log_path.open("w") as logfh:
        worker = subprocess.Popen(
            cmd,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(ROOT),
            start_new_session=True,
        )
    try:
        t_sample_start = time.monotonic()
        sampler = threading.Thread(
            target=sample_loop,
            args=(psutil.Process(worker.pid), samples, stop_sample, args.interval),
            daemon=True,
        )
        sampler.start()
        pub = threading.Thread(
            target=mod.publish_loop,
            args=(specs, args.backlog, done_fn, stop_pub, state),
            daemon=True,
        )
        pub.start()

        t_ready = time.monotonic() + args.ready_timeout
        while done_fn() == 0:
            if time.monotonic() > t_ready:
                msg = f"{args.config}: nothing completed within {args.ready_timeout}s (see {log_path})"
                raise RuntimeError(msg)
            if worker.poll() is not None:
                msg = f"{args.config}: worker exited rc={worker.returncode} (see {log_path})"
                raise RuntimeError(msg)
            time.sleep(0.2)

        time.sleep(args.warmup)
        d0, tw0 = done_fn(), time.monotonic()
        time.sleep(args.duration)
        d1, tw1 = done_fn(), time.monotonic()

        stop_pub.set()
        stop_sample.set()
        sampler.join(timeout=2.0)

        completed, window = d1 - d0, tw1 - tw0
        if completed <= 0:
            msg = f"{args.config}: no tasks completed during the {args.duration}s window"
            raise RuntimeError(msg)

        # sample_loop stamps t relative to its own start, not to the ready moment.
        in_window = [s for s in samples if tw0 <= t_sample_start + s["t"] <= tw1]
        summary = {
            "config": args.config,
            "framework": args.framework,
            "harness": HARNESS,
            "mode": "duration",
            "python": args.python_label,
            "slots": args.slots,
            "duration_seconds": args.duration,
            "n_tasks": completed,
            "n_completed": completed,
            "n_published": state["published"],
            "n_stranded": 0,
            "stalled": False,
            "complete_seconds": round(window, 3),
            "throughput_tps": round(completed / window, 1),
            "worker_argv": cmd,
            "env": {k: v for k, v in os.environ.items() if k.startswith("FW_")},
            "samples": in_window,
            "summary": summarize(in_window),
        }
        gil = None
        for line in log_path.read_text(errors="replace").splitlines():
            if "gil_enabled:" in line:
                gil = line.rsplit("gil_enabled:", 1)[1].strip() == "True"
                break
        summary["gil_enabled"] = gil
        args.output.write_text(json.dumps(summary, indent=2))
        st = summary["summary"]
        print(
            f"[runner_fw] {args.config}: {completed} tasks in {window:.1f}s, "
            f"{summary['throughput_tps']} tps, peak_rss={st['peak_rss_mb']} MB, "
            f"peak_pss={st['peak_pss_mb']} MB, mean_cpu={st['mean_cpu_pct']}%",
            flush=True,
        )
    finally:
        stop_pub.set()
        stop_sample.set()
        try:
            os.killpg(os.getpgid(worker.pid), signal.SIGTERM)
            worker.wait(timeout=20)
        except ProcessLookupError, subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()
