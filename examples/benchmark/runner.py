"""Run one benchmark configuration.

This script is invoked once per (flavor x python x pool config x variant)
combination. It:

  1. Starts a celery worker as a subprocess pinned to CPUs 0,1 via taskset
     (so the worker sees a "2-CPU server" regardless of host).
  2. Waits for the worker to be ready (polls celery inspect ping).
  3. Samples CPU%, RSS and PSS of the worker process tree every 0.5s.
  4. Publishes the workload, then polls AsyncResult.ready() for all tasks.
  5. On completion, stops the worker and writes a JSON results file.

Run from this directory (so PYTHONPATH=. resolves celeryapp / tasks):

    PYTHONPATH=. python runner.py \
        --config aio-async-c50 \
        --workload workload.json \
        --output results/aio-async-c50.json \
        --pool asyncio \
        --pool-opts loop_workers=2,loop_concurrency=50,sync_workers=2 \
        --variant async \
        --worker-bin /path/to/celery
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import _versions
import psutil


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.1)
    msg = f"timed out connecting to {host}:{port}"
    raise RuntimeError(msg)


def _pss(p: psutil.Process) -> int:
    """Proportional set size, or RSS where the kernel does not report it.

    Summing RSS over a prefork tree counts every copy-on-write page the parent
    shares with its children once per child, which on a 100-process pool roughly
    doubles the reported footprint. PSS divides each shared page by the number of
    processes mapping it, so the total is what the pool actually costs the host.
    Single-process pools are unaffected: with nothing shared, PSS tracks RSS.
    """
    try:
        return p.memory_full_info().pss
    except AttributeError, psutil.Error:
        return p.memory_info().rss


def sample_loop(proc: psutil.Process, samples: list[dict], stop: threading.Event, interval: float) -> None:
    """Sample CPU% and RSS of `proc` plus all its descendants every `interval` seconds.

    cpu_percent(interval=None) computes the delta against the *previous* call
    on the same psutil.Process object. We therefore cache Process objects by
    PID across iterations — recreating them each cycle (as the old code did)
    meant every fresh child object had no prior baseline, so cpu_percent
    returned 0 on its first call and the prefork pool's children were
    severely undercounted.

    New children discovered mid-run are primed on first sight (their first
    cpu_percent reading is dropped) so subsequent samples include them
    correctly. Dead PIDs are evicted from the cache.
    """
    cache: dict[int, psutil.Process] = {proc.pid: proc}
    # Prime the parent so its first sample reads a real value.
    try:
        proc.cpu_percent(interval=None)
    except psutil.NoSuchProcess:
        return

    def refresh() -> list[psutil.Process]:
        try:
            children = proc.children(recursive=True)
        except psutil.NoSuchProcess:
            return []
        live_pids = {proc.pid} | {c.pid for c in children}
        # Evict dead.
        for pid in list(cache):
            if pid not in live_pids:
                cache.pop(pid, None)
        # Add new (and prime them — discard the meaningless first reading).
        for c in children:
            if c.pid not in cache:
                cache[c.pid] = c
                try:
                    c.cpu_percent(interval=None)
                except psutil.NoSuchProcess:
                    cache.pop(c.pid, None)
        return list(cache.values())

    t0 = time.monotonic()
    while not stop.is_set():
        procs = refresh()
        if not procs:
            return

        cpu_total = 0.0
        rss_total = 0
        pss_total = 0
        n = 0
        for p in procs:
            try:
                cpu_total += p.cpu_percent(interval=None)
                rss_total += p.memory_info().rss
                pss_total += _pss(p)
                n += 1
            except psutil.NoSuchProcess, psutil.AccessDenied:
                pass

        samples.append(
            {
                "t": round(time.monotonic() - t0, 3),
                "cpu_pct": round(cpu_total, 1),
                "rss_mb": round(rss_total / (1024 * 1024), 1),
                "pss_mb": round(pss_total / (1024 * 1024), 1),
                "n_procs": n,
            },
        )
        stop.wait(interval)


def build_worker_cmd(
    args: argparse.Namespace,
    queue: str,
    log_path: Path,
) -> list[str]:
    """Build the celery worker command line, wrapped in taskset for CPU pinning."""
    cmd: list[str] = []
    if args.taskset and shutil.which("taskset"):
        cmd += ["taskset", "-c", args.taskset]
    cmd += [args.worker_bin, "-A", "celeryapp", "worker"]
    cmd += ["-Q", queue]
    # Need info-level so the "ready." line we detect appears in the log.
    cmd += ["--loglevel=info"]
    cmd += ["--logfile", str(log_path)]
    cmd += ["-n", f"bench-{os.getpid()}@%h"]
    if args.pool:
        cmd += ["--pool", args.pool]
    if args.concurrency is not None:
        cmd += ["--concurrency", str(args.concurrency)]
    if args.loop_workers is not None:
        cmd += ["--loop-workers", str(args.loop_workers)]
    if args.loop_concurrency is not None:
        cmd += ["--loop-concurrency", str(args.loop_concurrency)]
    if args.sync_workers is not None:
        cmd += ["--sync-workers", str(args.sync_workers)]
    return cmd


def wait_for_worker_ready(log_path: Path, timeout: float) -> bool:
    """Tail the worker log until the "ready." line appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            if " ready." in text:
                return True
            if "Unrecoverable error" in text or "Traceback" in text:
                return False
        time.sleep(0.2)
    return False


def import_tasks() -> tuple[Any, Any, Any]:
    """Import the app + tasks from the bench directory."""
    sys.path.insert(0, str(Path(__file__).parent))
    from celeryapp import app
    from tasks import mixed_async, mixed_sync

    return app, mixed_sync, mixed_async


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="config label for results file")
    ap.add_argument("--workload", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--worker-bin", required=True, help="path to the celery executable in the target venv")
    ap.add_argument("--pool", default=None, help='"asyncio" (celery-asyncio default), "prefork", "threads", "solo"')
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--loop-workers", type=int, default=None)
    ap.add_argument("--loop-concurrency", type=int, default=None)
    ap.add_argument("--sync-workers", type=int, default=None)
    ap.add_argument("--variant", choices=("sync", "async", "mixed"), default="sync")
    ap.add_argument(
        "--taskset",
        default="0,1,2,3",
        help='cores to pin worker to (passed as taskset -c); "" to disable',
    )
    ap.add_argument("--sample-interval", type=float, default=0.5)
    ap.add_argument("--ready-timeout", type=float, default=30.0)
    ap.add_argument("--run-timeout", type=float, default=900.0)
    ap.add_argument(
        "--stall-seconds",
        type=float,
        default=15.0,
        help="if n_done has not advanced for this long AND >=99%% complete, declare done (kombu-asyncio leaks ~0.2%% of tasks).",
    )
    ap.add_argument("--queue", default="bench")
    args = ap.parse_args()

    workload_doc = json.loads(args.workload.read_text())
    tasks_spec = workload_doc["tasks"]
    n_tasks = len(tasks_spec)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".worker.log")

    # Sanity-check the broker is up.
    wait_for_port("localhost", 6379, timeout=5)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    # Make sure imports prefer the venv's celery.
    env["PYTHONUNBUFFERED"] = "1"

    cmd = build_worker_cmd(args, queue=args.queue, log_path=log_path)
    print(f"[runner] starting worker: {' '.join(cmd)}", flush=True)
    # New session so we can kill the whole process group on exit, including
    # taskset wrapper + the worker's child loop threads. Without this, if the
    # runner is killed mid-flight the worker can survive and silently keep
    # consuming tasks from later benchmark runs.
    worker = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).parent),
        start_new_session=True,
    )

    try:
        ready = wait_for_worker_ready(log_path, timeout=args.ready_timeout)
        if not ready:
            msg = f"worker did not become ready within {args.ready_timeout}s (see {log_path})"
            if worker.poll() is not None and not log_path.exists():
                # Nothing was ever logged and the process is already gone, so
                # it died before celery got a chance to run. Usually the venv:
                # a console script keeps the absolute path it was built at, so
                # moving the checkout leaves the shebang pointing nowhere.
                msg = (
                    f"worker exited with {worker.returncode} before logging anything. "
                    f"Check that {args.worker_bin} still runs on its own; if this "
                    f"checkout was moved or renamed, re-run setup_venvs.sh."
                )
            raise RuntimeError(msg)

        app, mixed_sync, mixed_async = import_tasks()

        # Start resource sampling thread.
        ps_proc = psutil.Process(worker.pid)
        samples: list[dict] = []
        stop = threading.Event()
        sampler = threading.Thread(
            target=sample_loop,
            args=(ps_proc, samples, stop, args.sample_interval),
            daemon=True,
        )
        sampler.start()

        # Publish.
        def task_for(idx: int):
            if args.variant == "sync":
                return mixed_sync
            if args.variant == "async":
                return mixed_async
            return mixed_async if idx % 2 == 0 else mixed_sync

        t_publish_start = time.monotonic()
        results = []
        for i, t in enumerate(tasks_spec):
            results.append(task_for(i).apply_async(kwargs=t["kwargs"], queue=args.queue))
        t_publish_end = time.monotonic()

        # Poll for completion. ready() is a redis GET — cheap.
        # Some kombu-asyncio versions strand a small fraction of tasks in the
        # broker (queue-index has them, consumer never picks them up). If
        # progress stalls near 100%, declare done and record the leak.
        deadline = time.monotonic() + args.run_timeout
        n_done = 0
        prev_done = -1
        last_change = time.monotonic()
        last_print = 0.0
        stalled = False
        while n_done < n_tasks:
            if time.monotonic() > deadline:
                msg = f"workload did not complete within {args.run_timeout}s ({n_done}/{n_tasks})"
                raise RuntimeError(msg)
            n_done = sum(1 for r in results if r.ready())
            now = time.monotonic()
            if n_done != prev_done:
                prev_done = n_done
                last_change = now
            if n_done >= int(n_tasks * 0.99) and now - last_change > args.stall_seconds and n_done < n_tasks:
                stalled = True
                break
            if now - last_print > 2.0:
                print(f"[runner] {n_done}/{n_tasks} done", flush=True)
                last_print = now
            if n_done < n_tasks:
                time.sleep(0.25)
        t_complete = time.monotonic()

        stop.set()
        sampler.join(timeout=2.0)

        # Confirm one task result for sanity (pick a completed one).
        sample_result = None
        for r in results:
            if r.ready():
                try:
                    sample_result = r.get(timeout=5)
                except Exception:
                    pass
                break

        summary = {
            "config": args.config,
            "variant": args.variant,
            "pool": args.pool,
            "concurrency": args.concurrency,
            "loop_workers": args.loop_workers,
            "loop_concurrency": args.loop_concurrency,
            "sync_workers": args.sync_workers,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            # Which celery/kombu actually produced the number, recorded at run
            # time. Probing later reads whatever the venv holds by then, and a
            # result that outlives one setup_venvs.sh becomes unattributable.
            "versions": _versions.probe(_versions.venv_for(args.config)),
            "worker_bin": args.worker_bin,
            "taskset": args.taskset,
            "n_tasks": n_tasks,
            "n_completed": n_done,
            "n_stranded": n_tasks - n_done,
            "stalled": stalled,
            "publish_seconds": round(t_publish_end - t_publish_start, 3),
            "complete_seconds": round(t_complete - t_publish_start, 3),
            "throughput_tps": round(n_done / (t_complete - t_publish_start), 1),
            "sample_result": sample_result,
            "samples": samples,
            "summary": _summary(samples),
        }

        args.output.write_text(json.dumps(summary, indent=2))
        s = summary["summary"]
        stall_note = f" (STALLED: {summary['n_stranded']} stranded)" if stalled else ""
        print(
            f"[runner] {args.config}: {summary['complete_seconds']}s, "
            f"{summary['throughput_tps']} tps, "
            f"peak_rss={s['peak_rss_mb']} MB, peak_pss={s['peak_pss_mb']} MB, "
            f"mean_cpu={s['mean_cpu_pct']}%{stall_note}",
            flush=True,
        )

    finally:
        # Signal the whole process group so taskset + the worker + any
        # subprocesses go down together.
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError, PermissionError:
            worker.send_signal(signal.SIGTERM)
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError, PermissionError:
                worker.kill()
            worker.wait(timeout=5)


def _summary(samples: list[dict]) -> dict:
    if not samples:
        return {
            "peak_rss_mb": 0,
            "mean_rss_mb": 0,
            "peak_pss_mb": 0,
            "mean_pss_mb": 0,
            "peak_cpu_pct": 0,
            "mean_cpu_pct": 0,
        }
    rss = [s["rss_mb"] for s in samples]
    # Results predating PSS sampling have no such key; fall back so an old
    # JSON still summarises instead of raising.
    pss = [s.get("pss_mb", s["rss_mb"]) for s in samples]
    cpu = [s["cpu_pct"] for s in samples]
    return {
        "peak_rss_mb": max(rss),
        "mean_rss_mb": round(sum(rss) / len(rss), 1),
        "peak_pss_mb": max(pss),
        "mean_pss_mb": round(sum(pss) / len(pss), 1),
        "peak_cpu_pct": max(cpu),
        "mean_cpu_pct": round(sum(cpu) / len(cpu), 1),
        "n_samples": len(samples),
        "peak_procs": max(s.get("n_procs", 1) for s in samples),
    }


if __name__ == "__main__":
    main()
