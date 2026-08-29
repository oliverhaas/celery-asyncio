"""Regenerate RESULTS.md from the JSONs in results/: `python render_results.py --write`."""

# Everything mechanical (environment, versions, the per-profile tables, the cost
# tables) comes out of the result JSONs. The interpretive prose does not:
# sections wrapped in <!-- keep:name --> .. <!-- /keep --> are lifted verbatim
# out of the existing RESULTS.md and put back, so a refresh never costs the
# analysis that was written around the old numbers.

from __future__ import annotations

import argparse
import json
import math
import platform
import re
import subprocess
from collections import Counter
from pathlib import Path

import _versions
import workload

ROOT = Path(__file__).resolve().parent
RESULTS_MD = ROOT / "RESULTS.md"

# Fargate Linux/x86 us-east-1, May 2026.
VCPU_S_RATE = 0.04048 / 3600  # $/vCPU·s
GB_S_RATE = 0.004445 / 3600  # $/GB·s

PROFILES = ("mixed", "cpu-only", "io-only")

# Stable canonical order; each table sorts itself.
CANONICAL_CONFIGS = [
    "aio-async-l4c25-314",
    "aio-async-l4c25-314t",
    "aio-async-l4c25-uvloop-314",
    "aio-async-l4c25-uvloop-314t",
    "aio-sync-s4-314",
    "aio-sync-s4-314t",
    "aio-sync-s4-uvloop-314",
    "aio-sync-s4-uvloop-314t",
    "aio-mixed-l2c50-s2-314",
    "aio-mixed-l2c50-s2-314t",
    "aio-mixed-l2c50-s2-uvloop-314",
    "aio-mixed-l2c50-s2-uvloop-314t",
    "classic-prefork1-314",
    "classic-prefork1-314t",
    "classic-prefork4-314",
    "classic-prefork4-314t",
    "classic-threads4-314",
    "classic-threads4-314t",
]

WORKER_SHAPES = {
    "prefork1": "prefork × 1",
    "prefork4": "prefork × 4",
    "threads4": "threads × 4",
    "async-l4c25": "4 loop × 25 + 1 sync",
    "sync-s4": "4 sync threads",
    "mixed-l2c50-s2": "2 loop × 50 + 2 sync",
}

# Number of OS processes a config runs, for the per-process memory model.
PROCESS_COUNTS = {"prefork4": 4, "prefork1": 1}

KEEP_RE = re.compile(r"<!-- keep:(?P<name>[\w-]+) -->\n(?P<body>.*?)\n<!-- /keep -->", re.DOTALL)


_LOADED: dict[str, tuple[list[dict], list[str]]] = {}


def _read_profile(profile: str) -> tuple[list[dict], list[str]]:
    slug = profile.replace("-", "_")
    runs = []
    missing = []
    for config in CANONICAL_CONFIGS:
        f = ROOT / "results" / f"{config}-{slug}.json"
        if f.exists():
            runs.append(json.loads(f.read_text()))
        else:
            missing.append(config)
    if not runs:
        return [], [f"no results at all ({len(missing)} configs missing)"]

    # One profile is one workload driven against every config, so every run in it
    # must cover the same n_tasks. A leftover from an earlier smoke run has fewer,
    # finishes in a fraction of the wall-clock, and would otherwise sort straight
    # to the top of the table as if it were the fastest config in the matrix.
    # The full count is the anchor rather than the most common one: a profile the
    # matrix is still working through can hold more stale files than fresh ones.
    expected = max(Counter(r["n_tasks"] for r in runs))
    notes = [
        f"`{r['config']}` dropped: {r['n_tasks']} tasks, not {expected} (stale run)"
        for r in runs
        if r["n_tasks"] != expected
    ]
    notes += [f"`{config}` missing" for config in missing]
    return [r for r in runs if r["n_tasks"] == expected], notes


def load_profile(profile: str) -> list[dict]:
    if profile not in _LOADED:
        _LOADED[profile] = _read_profile(profile)
    return _LOADED[profile][0]


def profile_notes(profile: str) -> list[str]:
    """What `load_profile` left out, so the report can say so rather than imply completeness."""
    load_profile(profile)
    return _LOADED[profile][1]


def _describe_workers(config: str) -> str:
    for key, shape in WORKER_SHAPES.items():
        if key in config:
            return shape
    return ""


def _process_count(config: str) -> int:
    for key, n in PROCESS_COUNTS.items():
        if key in config:
            return n
    return 1


def _ideal_cost_per_million(r: dict) -> float:
    """$/1M tasks using mean CPU + mean RSS over the run wall."""
    s = r["summary"]
    cpu_seconds = (s["mean_cpu_pct"] / 100) * r["complete_seconds"]
    mem_gb_seconds = (s["mean_rss_mb"] / 1024) * r["complete_seconds"]
    return (cpu_seconds * VCPU_S_RATE + mem_gb_seconds * GB_S_RATE) / r["n_completed"] * 1_000_000


def _provisioned_slot(r: dict) -> tuple[float, float]:
    """(vCPU, GB) for a Fargate slot sized to peak load + 25% memory headroom."""
    s = r["summary"]
    peak_vcpu = s["peak_cpu_pct"] / 100
    vcpu = next((slot for slot in (0.25, 0.5, 1, 2, 4, 8, 16) if peak_vcpu <= slot), 16)
    # Fargate memory comes in 0.5 GiB increments.
    return vcpu, math.ceil(s["peak_rss_mb"] * 1.25 / 1024 * 2) / 2


def _provisioned_cost_per_million(r: dict) -> float:
    vcpu, gb = _provisioned_slot(r)
    return (vcpu * VCPU_S_RATE + gb * GB_S_RATE) * r["complete_seconds"] / r["n_completed"] * 1_000_000


def _modelled_cost_per_million(r: dict, rss_mb_per_process: float) -> float:
    """Idealised cost with measured RSS swapped for a modelled per-process figure."""
    s = r["summary"]
    cpu_seconds = (s["mean_cpu_pct"] / 100) * r["complete_seconds"]
    total_rss_mb = rss_mb_per_process * _process_count(r["config"])
    mem_gb_seconds = (total_rss_mb / 1024) * r["complete_seconds"]
    return (cpu_seconds * VCPU_S_RATE + mem_gb_seconds * GB_S_RATE) / r["n_completed"] * 1_000_000


def _table(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    sep = ["---:" if a == "r" else "---" for a in aligns]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def main_table(profile: str) -> str:
    rows = sorted(load_profile(profile), key=lambda r: r["complete_seconds"])
    body = []
    for r in rows:
        s = r["summary"]
        body.append(
            [
                r["config"],
                "3.14t" if r["config"].endswith("-314t") else "3.14",
                r["variant"],
                _describe_workers(r["config"]),
                f"{r['complete_seconds']:.1f} s",
                f"{r['throughput_tps']:.1f}",
                f"{s['peak_rss_mb']:.0f} MB",
                f"{s['mean_rss_mb']:.0f} MB",
                f"{s['peak_cpu_pct']:.0f} %",
                f"{s['mean_cpu_pct']:.0f} %",
                str(r["n_stranded"]),
            ]
        )
    return _table(
        [
            "config",
            "py",
            "variant",
            "workers",
            "wall",
            "TPS",
            "peak RSS",
            "mean RSS",
            "peak CPU",
            "mean CPU",
            "stranded",
        ],
        ["l", "l", "l", "l", "r", "r", "r", "r", "r", "r", "r"],
        body,
    )


def cost_table(profile: str) -> str:
    rows = sorted(load_profile(profile), key=_ideal_cost_per_million)
    body = []
    for r in rows:
        s = r["summary"]
        vcpu_s_per_1k = (s["mean_cpu_pct"] / 100) * r["complete_seconds"] / r["n_completed"] * 1000
        mb_s_per_1k = s["mean_rss_mb"] * r["complete_seconds"] / r["n_completed"] * 1000
        vcpu_slot, gb_slot = _provisioned_slot(r)
        body.append(
            [
                r["config"],
                f"{r['throughput_tps']:.1f}",
                f"{vcpu_s_per_1k:.2f}",
                f"{mb_s_per_1k:.0f}",
                f"${_ideal_cost_per_million(r):.2f}",
                f"${_provisioned_cost_per_million(r):.2f}",
                f"{vcpu_slot:g} vCPU / {gb_slot:g} GB",
            ]
        )
    return _table(
        ["config", "TPS", "vCPU·s / 1 k", "MB·s / 1 k", "ideal $/1M", "prov $/1M", "Fargate slot"],
        ["l", "r", "r", "r", "r", "r", "r"],
        body,
    )


def modelled_table(profile: str, rss_mb: float) -> str:
    """Cost ranking once every worker process is assumed to hold `rss_mb`."""
    rows = load_profile(profile)
    rows.sort(key=lambda r: _modelled_cost_per_million(r, rss_mb))
    body = []
    for r in rows:
        procs = _process_count(r["config"])
        body.append(
            [
                r["config"],
                str(procs),
                f"{rss_mb * procs:,.0f} MB".replace(",", " "),
                f"{r['throughput_tps']:.1f}",
                f"${_modelled_cost_per_million(r, rss_mb):.3f}",
            ]
        )
    return _table(
        ["config", "procs", "RSS modelled", "TPS", f"ideal $/1M @ {rss_mb:.0f} MB"],
        ["l", "r", "r", "r", "r"],
        body,
    )


def crossover_table(profile: str, rss_values: tuple[int, ...] = (100, 800, 1500, 2500, 5000)) -> str:
    """Best prefork config against best aio config as per-process RSS grows."""
    rows = load_profile(profile)
    prefork = [r for r in rows if "prefork4" in r["config"]]
    aio = [r for r in rows if r["config"].startswith("aio-")]
    if not prefork or not aio:
        return "_(not enough results to compute a crossover.)_"
    best_prefork = min(prefork, key=_ideal_cost_per_million)
    best_aio = min(aio, key=_ideal_cost_per_million)
    body = []
    for rss in rss_values:
        p = _modelled_cost_per_million(best_prefork, rss)
        a = _modelled_cost_per_million(best_aio, rss)
        if abs(p - a) / max(p, a) < 0.01:
            winner = "tied"
        else:
            cheaper, ratio = ("aio", p / a) if a < p else ("prefork", a / p)
            winner = f"{cheaper} ({ratio:.2f} ×)"
        body.append([f"{rss:,} MB".replace(",", " "), f"${p:.3f}", f"${a:.3f}", winner])
    table = _table(
        ["RSS / process", f"{best_prefork['config']} $/1M", f"{best_aio['config']} $/1M", "winner"],
        ["r", "r", "r", "l"],
        body,
    )
    procs = _process_count(best_prefork["config"])
    return (
        f"{table}\n\n(`{best_prefork['config']}` is {procs} processes and pays {procs} × RSS; aio is single-process.)"
    )


def keeps(text: str) -> dict[str, str]:
    return {m.group("name"): m.group("body") for m in KEEP_RE.finditer(text)}


def keep_block(name: str, existing: dict[str, str], placeholder: str) -> str:
    body = existing.get(name, placeholder)
    return f"<!-- keep:{name} -->\n{body}\n<!-- /keep -->"


def _environment(rows: list[dict]) -> list[str]:
    """One bullet per distinct toolchain the matrix ran on."""
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(_versions.venv_for(r["config"]), _versions.versions_for(r))
    lines = []
    for venv, v in sorted(seen.items()):
        if not v:
            continue
        gil = "free-threaded" if v.get("free_threading") else "GIL"
        parts = [f"CPython {v.get('python', '?')} ({gil})"]
        parts += [str(v[k]) for k in ("celery", "kombu", "redis", "uvloop") if v.get(k)]
        lines.append(f"- `{venv}`: " + ", ".join(parts))
    broker = _versions.broker_version()
    if broker:
        lines.append(f"- Broker / backend: {broker} on `localhost:6379`")
    lines.append(f"- Host: {platform.platform()}")
    return lines


def _profile_bullets() -> list[str]:
    lines = []
    mix = ", ".join(f"{name} {p['weight']} %" for name, p in workload.MIXED_PROFILES.items())
    lines.append(f"- `mixed`: balanced mix exercising all three dimensions: {mix}.")
    cpu = workload.CPU_ONLY_DEFAULT
    lines.append(
        f"- `cpu-only`: 100 % cpu_heavy (`cpu_iters={cpu['cpu_iters']:,}`). Stresses the GIL.".replace(",", " ")
    )
    io = workload.IO_ONLY_DEFAULT
    lines.append(f"- `io-only`: 100 % I/O sleep (`io_seconds={io['io_seconds']}`). Stresses concurrency.")
    return lines


def _git_date() -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--format=%cs"], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() or "unknown"


def render() -> str:
    existing = keeps(RESULTS_MD.read_text()) if RESULTS_MD.exists() else {}
    all_rows = [r for p in PROFILES for r in load_profile(p)]
    counts = {p: len(load_profile(p)) for p in PROFILES}
    total = sum(counts.values())
    stranded = sum(r["n_stranded"] for r in all_rows)

    parts = [
        "# Benchmark Results",
        "",
        f"Generated by `render_results.py` from `results/*.json`, repo at {_git_date()}.",
        f"{total} of {len(CANONICAL_CONFIGS) * len(PROFILES)} runs present"
        f" ({', '.join(f'{p}: {n}' for p, n in counts.items())}); {stranded} stranded task(s) in total.",
        "",
        "## Environment",
        "",
        *_environment(all_rows),
        "- Worker pinned to 4 cores via `taskset -c 0,1,2,3`; the driver runs off those cores.",
        "",
        "## Workload profiles",
        "",
        *_profile_bullets(),
        "",
        "Each profile runs against every config, under both the default CPython"
        " selector loop and uvloop (`BENCH_UVLOOP=1`). Every worker logs its"
        " actual loop class at startup, so activation is checked rather than assumed.",
        "",
        "## Methodology notes",
        "",
        keep_block(
            "methodology",
            existing,
            "_Hand-written; survives regeneration._",
        ),
    ]

    for profile in PROFILES:
        rows = load_profile(profile)
        notes = profile_notes(profile)
        parts += [
            "",
            f"## Full results, {profile} workload",
            "",
            f"{len(rows)} of {len(CANONICAL_CONFIGS)} configs, {rows[0]['n_tasks']:,}".replace(",", " ")
            + " tasks each, sorted by wall-clock."
            if rows
            else "No results yet.",
            "",
            main_table(profile),
        ]
        if notes:
            parts += ["", "Not in the table above:", "", *[f"- {note}" for note in notes]]
        parts += ["", "### Observations", "", keep_block(f"obs-{profile}", existing, "_Not written yet for this run._")]

    parts += [
        "",
        "## Cost per task (AWS Fargate, Linux/x86, us-east-1)",
        "",
        f"Pricing: ${VCPU_S_RATE * 3600:.5f} per vCPU·h, ${GB_S_RATE * 3600:.6f} per GB·h.",
        "",
        "- **Idealised**: pay only for what is consumed: `(mean_cpu × wall × vCPU·s) + (mean_rss × wall × GB·s)`.",
        "- **Provisioned**: a slot sized to peak: CPU rounded up to"
        " {0.25, 0.5, 1, 2, 4, 8, 16} vCPU from `peak_cpu`, memory to the next"
        " 0.5 GiB above `peak_rss × 1.25`.",
    ]
    for profile in PROFILES:
        parts += ["", f"### {profile}, sorted by idealised cost", "", cost_table(profile)]

    parts += [
        "",
        "### Mixed, modelled at 800 MB per worker process",
        "",
        "The bench app is bare-bones. A real Django/Celery worker with ORM cache,"
        " app code and framework imports loaded sits closer to 800 MB, and prefork"
        " pays that per child.",
        "",
        modelled_table("mixed", 800),
        "",
        "### Crossover memory (mixed)",
        "",
        crossover_table("mixed"),
        "",
        "## Summary",
        "",
        keep_block("summary", existing, "_Not written yet for this run._"),
        "",
        "## Known issues surfaced",
        "",
        keep_block("known-issues", existing, "_Not written yet for this run._"),
        "",
        "## History",
        "",
        keep_block("history", existing, "_Why earlier revisions of this file reported different numbers._"),
        "",
    ]
    return "\n".join(parts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="overwrite RESULTS.md instead of printing")
    args = ap.parse_args()
    out = render()
    if args.write:
        RESULTS_MD.write_text(out)
        print(f"wrote {RESULTS_MD}")
    else:
        print(out)
