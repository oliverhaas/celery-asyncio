"""Measurement pieces shared by the celery runner and the framework runner.

Kept in one module so the two runners cannot drift. A cross-framework table is
only meaningful if every row was sampled by the same code, and the fastest way
to lose that property is to copy the sampler.
"""

import threading
import time

import psutil

# Bumped whenever a change makes results incomparable with earlier ones.
# 5: rss and pss read together from smaps_rollup, so PSS cannot exceed RSS.
HARNESS = 5


def read_memory(p: psutil.Process) -> tuple[int, int]:
    """RSS and PSS for one process, read from a single kernel snapshot.

    Summing RSS over a prefork tree counts every copy-on-write page the parent
    shares with its children once per child, which on a 100-process pool roughly
    doubles the reported footprint. PSS divides each shared page by the number of
    processes mapping it, so the total is what the pool actually costs the host.

    smaps_rollup carries both figures in one file. psutil reads rss from statm
    and pss from smaps, and a worker allocating 8 MiB task buffers moves between
    those two reads, which produced samples whose PSS exceeded their own RSS.
    """
    try:
        rss = pss = 0
        with open(f"/proc/{p.pid}/smaps_rollup") as fh:
            for line in fh:
                if line.startswith("Rss:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("Pss:"):
                    pss = int(line.split()[1]) * 1024
                    break
        if rss:
            return rss, pss or rss
    except OSError, ValueError:
        pass
    info = p.memory_info()
    return info.rss, info.rss


def sample_loop(proc: psutil.Process, samples: list[dict], stop: threading.Event, interval: float) -> None:
    """Sample CPU% and RSS of `proc` plus all its descendants every `interval` seconds.

    cpu_percent(interval=None) computes the delta against the *previous* call
    on the same psutil.Process object. We therefore cache Process objects by
    PID across iterations: recreating them each cycle meant every fresh child
    object had no prior baseline, so cpu_percent returned 0 on its first call
    and a prefork pool's children were severely undercounted.

    New children discovered mid-run are primed on first sight (their first
    cpu_percent reading is dropped) so subsequent samples include them
    correctly. Dead PIDs are evicted from the cache.
    """
    cache: dict[int, psutil.Process] = {proc.pid: proc}
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
        for pid in list(cache):
            if pid not in live_pids:
                cache.pop(pid, None)
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
                rss, pss = read_memory(p)
                rss_total += rss
                pss_total += pss
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


def summarize(samples: list[dict]) -> dict:
    if not samples:
        return {
            "peak_rss_mb": 0,
            "mean_rss_mb": 0,
            "peak_pss_mb": 0,
            "mean_pss_mb": 0,
            "peak_cpu_pct": 0,
            "mean_cpu_pct": 0,
            "n_samples": 0,
            "peak_procs": 0,
        }
    rss = [s["rss_mb"] for s in samples]
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
        "peak_procs": max(s["n_procs"] for s in samples),
    }
