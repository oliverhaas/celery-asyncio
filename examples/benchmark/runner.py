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
from bench_core import HARNESS, sample_loop, summarize


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


def _worker_loop(boot_path: Path) -> str | None:
    """The event-loop class the worker process reported at startup, if it said."""
    if not boot_path.exists():
        return None
    for line in boot_path.read_text(errors="replace").splitlines():
        if "event loop:" in line:
            return line.rsplit("event loop:", 1)[1].strip()
    return None


def run_steady_state(
    app: Any,
    tasks_spec: list[dict],
    task_for: Any,
    queue: str,
    duration: float,
    warmup: float,
    backlog: int,
) -> dict:
    """Publish continuously and count completions over a fixed window.

    A fixed task count gives the fastest configs the shortest measurement: at
    1400 TPS ten thousand tasks are gone in seven seconds, while a four-slot
    io-bound config spends twenty minutes on the identical work. Measuring for
    a fixed time instead gives every config the same statistical weight.

    Completions are counted with DBSIZE on the result backend, which is one
    command regardless of how many tasks are outstanding. Polling ready() per
    task would put the driver's own load in the middle of the measurement.
    """
    import redis

    client = redis.Redis.from_url(app.conf.result_backend)
    base = client.dbsize()
    published = 0
    n_spec = len(tasks_spec)
    stop_pub = threading.Event()

    def publisher() -> None:
        nonlocal published
        while not stop_pub.is_set():
            if published - (client.dbsize() - base) < backlog:
                for _ in range(500):
                    spec = tasks_spec[published % n_spec]
                    task_for(published).apply_async(kwargs=spec["kwargs"], queue=queue)
                    published += 1
            else:
                stop_pub.wait(0.02)

    pub_thread = threading.Thread(target=publisher, daemon=True)
    t_start = time.monotonic()
    pub_thread.start()

    time.sleep(warmup)
    done0, tw0 = client.dbsize() - base, time.monotonic()
    time.sleep(duration)
    done1, tw1 = client.dbsize() - base, time.monotonic()

    stop_pub.set()
    pub_thread.join(timeout=30)

    completed = done1 - done0
    window = tw1 - tw0
    if completed <= 0:
        msg = f"no tasks completed during the {duration}s window (worker dead or starved?)"
        raise RuntimeError(msg)
    return {
        "window_start": tw0 - t_start,
        "window_end": tw1 - t_start,
        "window_seconds": round(window, 3),
        "warmup_seconds": warmup,
        "n_completed_in_window": completed,
        "n_published": published,
        "n_outstanding_at_end": published - (client.dbsize() - base),
        "throughput_tps": round(completed / window, 1),
    }


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
        help="if n_done has not advanced for this long AND >=99%% complete, declare done (kombu leaks ~0.2%% of tasks).",
    )
    ap.add_argument("--queue", default="bench")
    ap.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="seconds to measure after warmup; 0 drains a fixed task count instead",
    )
    ap.add_argument("--warmup", type=float, default=10.0, help="seconds to discard before measuring")
    ap.add_argument("--backlog", type=int, default=4000, help="tasks kept outstanding so the pool never starves")
    args = ap.parse_args()

    workload_doc = json.loads(args.workload.read_text())
    tasks_spec = workload_doc["tasks"]
    n_tasks = len(tasks_spec)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".worker.log")
    # Celery appends, and readiness is detected by finding " ready." in this
    # file. A log left by an earlier run of the same config already contains
    # that line, so the wait returned at once, the workload was published into
    # a worker still booting, and its whole startup was billed to the result.
    # That is worth ~10 s on the asyncio pool, which is the wrong side of the
    # comparison this benchmark exists to make.
    log_path.unlink(missing_ok=True)

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
    # celeryapp announces the event-loop class it ended up with on stderr, and
    # discarding that stream made uvloop activation unverifiable for the process
    # actually running the tasks: the driver imports celeryapp too, so its own
    # line looks like confirmation while saying nothing about the worker.
    boot_path = args.output.with_suffix(".boot.log")
    with boot_path.open("w") as boot_log:
        worker = subprocess.Popen(
            cmd,
            env=env,
            stdout=boot_log,
            stderr=subprocess.STDOUT,
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

        if args.duration > 0:
            measured = run_steady_state(
                app,
                tasks_spec,
                task_for,
                args.queue,
                args.duration,
                args.warmup,
                args.backlog,
            )
            stop.set()
            sampler.join(timeout=2.0)
            # Warmup covers a pool still forking children and filling prefetch.
            window = [s for s in samples if measured["window_start"] <= s["t"] <= measured["window_end"]]
            summary = {
                "config": args.config,
                "harness": HARNESS,
                "mode": "duration",
                "variant": args.variant,
                "pool": args.pool,
                "concurrency": args.concurrency,
                "loop_workers": args.loop_workers,
                "loop_concurrency": args.loop_concurrency,
                "sync_workers": args.sync_workers,
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "versions": _versions.probe(_versions.venv_for(args.config)),
                "worker_loop": _worker_loop(boot_path),
                "worker_bin": args.worker_bin,
                "taskset": args.taskset,
                "duration_seconds": args.duration,
                "n_tasks": measured["n_completed_in_window"],
                "n_completed": measured["n_completed_in_window"],
                "n_stranded": 0,
                "stalled": False,
                "complete_seconds": measured["window_seconds"],
                "throughput_tps": measured["throughput_tps"],
                "steady_state": measured,
                "sample_result": None,
                "samples": window,
                "summary": summarize(window),
            }
            args.output.write_text(json.dumps(summary, indent=2))
            st = summary["summary"]
            print(
                f"[runner] {args.config}: {measured['n_completed_in_window']} tasks in "
                f"{measured['window_seconds']}s, {summary['throughput_tps']} tps, "
                f"peak_rss={st['peak_rss_mb']} MB, peak_pss={st['peak_pss_mb']} MB, "
                f"mean_cpu={st['mean_cpu_pct']}%",
                flush=True,
            )
            return

        t_publish_start = time.monotonic()
        results = []
        for i, t in enumerate(tasks_spec):
            results.append(task_for(i).apply_async(kwargs=t["kwargs"], queue=args.queue))
        t_publish_end = time.monotonic()

        # Poll for completion. ready() is a redis GET, which is cheap.
        # Some kombu versions strand a small fraction of tasks in the
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
            "harness": HARNESS,
            "mode": "fixed-count",
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
            "worker_loop": _worker_loop(boot_path),
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
            "summary": summarize(samples),
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


if __name__ == "__main__":
    main()
