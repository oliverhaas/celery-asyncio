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
    "classic-prefork25-314",
    "classic-prefork25-314t",
    "classic-threads25-314",
    "classic-threads25-314t",
    "classic-prefork100-314",
    "classic-prefork100-314t",
    "classic-threads100-314",
    "classic-threads100-314t",
]

# uvloop was measured at noise level on every profile, so it is a single
# comparison of its own rather than a doubled row in every table.
UVLOOP_BASE = "aio-async-l4c25"

# Results from an older harness are not comparable and must not share a table.
HARNESS_MIN = 5
MAIN_CONFIGS = [c for c in CANONICAL_CONFIGS if "-uvloop-" not in c]

# Shapes for the aio layouts, whose labels do not encode a single number.
# The classic ones are read off the label instead (see CLASSIC_RE), because
# substring matching cannot tell `prefork1` from `prefork100`.
WORKER_SHAPES = {
    "async-l4c25": "4 loop × 25 + 1 sync",
    "sync-s4": "4 sync threads",
    "mixed-l2c50-s2": "2 loop × 50 + 2 sync",
}

# Concurrent task slots each layout offers, for reading memory against
# concurrency. The aio pools carry one spare sync thread each.
AIO_SLOTS = {"async-l4c25": 101, "sync-s4": 4, "mixed-l2c50-s2": 102}

CLASSIC_RE = re.compile(r"classic-(?P<pool>prefork|threads)(?P<n>\d+)-")

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

    # Mixing measurement methods in one table is how earlier revisions went wrong.
    notes = [
        f"`{r['config']}` dropped: harness {r.get('harness', 0)}, not {HARNESS_MIN} (stale run)"
        for r in runs
        if r.get("harness", 0) < HARNESS_MIN
    ]
    runs = [r for r in runs if r.get("harness", 0) >= HARNESS_MIN]
    if not runs:
        return [], notes + [f"no results from harness {HARNESS_MIN}"]

    expected = max(Counter(r["duration_seconds"] for r in runs))
    notes += [
        f"`{r['config']}` dropped: {r['duration_seconds']}s window, not {expected}s"
        for r in runs
        if r["duration_seconds"] != expected
    ]
    notes += [f"`{config}` missing" for config in missing]
    return [r for r in runs if r["duration_seconds"] == expected], notes


def load_profile(profile: str, include_uvloop: bool = False) -> list[dict]:
    if profile not in _LOADED:
        _LOADED[profile] = _read_profile(profile)
    rows = _LOADED[profile][0]
    return rows if include_uvloop else [r for r in rows if "-uvloop-" not in r["config"]]


def profile_notes(profile: str) -> list[str]:
    """What `load_profile` left out, so the report can say so rather than imply completeness."""
    load_profile(profile)
    return _LOADED[profile][1]


def _classic(config: str) -> tuple[str, int] | None:
    m = CLASSIC_RE.search(config)
    return (m.group("pool"), int(m.group("n"))) if m else None


def _describe_workers(config: str) -> str:
    if (c := _classic(config)) is not None:
        return f"{c[0]} × {c[1]}"
    for key, shape in WORKER_SHAPES.items():
        if key in config:
            return shape
    return ""


def _slots(config: str) -> int:
    """Tasks the config can have in flight at once."""
    if (c := _classic(config)) is not None:
        return c[1]
    for key, n in AIO_SLOTS.items():
        if key in config:
            return n
    return 1


def _process_count(config: str) -> int:
    """OS processes the config runs: prefork forks one child per slot, the rest are threads."""
    c = _classic(config)
    return c[1] if c is not None and c[0] == "prefork" else 1


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


# (result label, framework, shape, slots, delivery). Two of these transports
# never acknowledge, so a dying worker drops its in-flight tasks.
FW_ROWS = [
    ("celery-aio", "celery-asyncio", "1 proc, 4 loop x 25 + 1 sync", 101, "at-least-once"),
    ("celery-aio-p4", "celery-asyncio", "4 proc x (1 loop x 25)", 100, "at-least-once"),
    ("celery-thr", "celery (upstream)", "1 proc, 100 threads", 100, "at-least-once"),
    ("celery-thr-p4", "celery (upstream)", "4 proc x 25 threads", 100, "at-least-once"),
    ("taskiq", "taskiq (list)", "4 proc x 25 async", 100, "at-most-once"),
    ("taskiq-stream", "taskiq (stream)", "4 proc x 25 async", 100, "at-least-once"),
    ("dramatiq", "dramatiq", "4 proc x 25 threads", 100, "at-least-once"),
    ("arq", "arq", "4 proc x 25 async", 100, "at-least-once"),
    ("djangoq", "django-q2", "32 processes", 32, "at-most-once"),
]

FW_VENVS = {
    "celery-asyncio": (".venv-async-314t", "celery_asyncio"),
    "celery (upstream)": (".venv-classic-314t", "celery"),
    "taskiq (list)": (".venv-taskiq-314t", "taskiq"),
    "taskiq (stream)": (".venv-taskiq-314t", "taskiq"),
    "dramatiq": (".venv-dramatiq-314t", "dramatiq"),
    "arq": (".venv-arq-314t", "arq"),
    "django-q2": (".venv-djangoq-314t", "django_q2"),
}


def framework_table() -> tuple[str, list[str]]:
    """One row per framework configuration, best of its repeats.

    Reporting the best run rather than a mean keeps every number in a row from
    the same measurement; the spread column says how much that choice mattered.
    """
    body, notes = [], []
    measured = []
    for label, name, shape, slots, delivery in FW_ROWS:
        files = sorted((ROOT / "results").glob(f"fw-{label}-[0-9].json"))
        runs = [json.loads(f.read_text()) for f in files]
        runs = [r for r in runs if r.get("harness", 0) >= HARNESS_MIN]
        if not runs:
            notes.append(f"`fw-{label}` missing")
            continue
        best = max(runs, key=lambda r: r["throughput_tps"])
        tps = [r["throughput_tps"] for r in runs]
        spread = (max(tps) - min(tps)) / (sum(tps) / len(tps)) * 100
        measured.append((name, best))
        s = best["summary"]
        venv, dist = FW_VENVS[name]
        found = sorted((ROOT / venv).glob(f"lib/*/site-packages/{dist}-*.dist-info"))
        version = found[-1].name.removesuffix(".dist-info").rsplit("-", 1)[-1] if found else "?"
        body.append(
            [
                name,
                version,
                shape,
                str(slots),
                delivery,
                f"{best['throughput_tps']:.0f}",
                f"{spread:.1f} %",
                f"{s['peak_rss_mb']:.0f} MB",
                f"{s['peak_pss_mb']:.0f} MB",
                f"{s['mean_cpu_pct']:.0f} %",
                f"{best['throughput_tps'] / (s['mean_cpu_pct'] / 100):.0f}",
                str(len(runs)),
            ],
        )
        if str(best.get("gil_enabled")) == "True":
            notes.append(f"`fw-{label}` ran with the GIL **enabled**; not comparable")
        if best["n_stranded"]:
            notes.append(f"`fw-{label}` stranded {best['n_stranded']} task(s)")
    body.sort(key=lambda row: -float(row[5]))
    table = _table(
        [
            "framework",
            "version",
            "shape",
            "slots",
            "delivery",
            "TPS",
            "spread",
            "peak RSS",
            "peak PSS",
            "mean CPU",
            "tasks/core·s",
            "runs",
        ],
        ["l", "l", "l", "r", "l", "r", "r", "r", "r", "r", "r", "r"],
        body,
    )
    return table, notes


def _table(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    sep = ["---:" if a == "r" else "---" for a in aligns]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def main_table(profile: str) -> str:
    rows = sorted(load_profile(profile), key=lambda r: -r["throughput_tps"])
    body = []
    for r in rows:
        s = r["summary"]
        body.append(
            [
                r["config"],
                "3.14t" if r["config"].endswith("-314t") else "3.14",
                r["variant"],
                _describe_workers(r["config"]),
                str(_slots(r["config"])),
                f"{r['n_tasks']:,}".replace(",", " "),
                f"{r['throughput_tps']:.1f}",
                f"{s['peak_rss_mb']:.0f} MB",
                _mb(s, "peak_pss_mb"),
                f"{s['mean_rss_mb']:.0f} MB",
                f"{s['peak_cpu_pct']:.0f} %",
                f"{s['mean_cpu_pct']:.0f} %",
                str(r["n_stranded"]),
            ],
        )
    return _table(
        [
            "config",
            "py",
            "variant",
            "workers",
            "slots",
            "tasks",
            "TPS",
            "peak RSS",
            "peak PSS",
            "mean RSS",
            "peak CPU",
            "mean CPU",
            "stranded",
        ],
        ["l", "l", "l", "l", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
        body,
    )


def _mb(summary: dict, key: str) -> str:
    """A memory figure, or a dash for runs recorded before that metric existed."""
    return f"{summary[key]:.0f} MB" if key in summary else "—"


def memory_table(profile: str) -> str:
    """Memory against concurrency, which is the only way prefork and threads compare fairly.

    RSS is summed over the process tree, so a prefork pool is charged for every
    copy-on-write page once per child. PSS is not, and is the column to read
    when comparing a 100-process pool against a single-process one.
    """
    rows = sorted(load_profile(profile), key=lambda r: (_slots(r["config"]), r["config"]))
    body = []
    for r in rows:
        s = r["summary"]
        slots = _slots(r["config"])
        per_slot = f"{s['peak_pss_mb'] / slots:.1f} MB" if "peak_pss_mb" in s else "—"
        body.append(
            [
                r["config"],
                str(slots),
                str(s.get("peak_procs", _process_count(r["config"]))),
                f"{s['peak_rss_mb']:.0f} MB",
                _mb(s, "peak_pss_mb"),
                per_slot,
                f"{r['throughput_tps']:.1f}",
            ],
        )
    return _table(
        ["config", "slots", "procs", "peak RSS", "peak PSS", "PSS / slot", "TPS"],
        ["l", "r", "r", "r", "r", "r", "r"],
        body,
    )


def uvloop_table() -> str:
    """stdlib selector loop against uvloop, same config, one row per build."""
    by_config = {r["config"]: r for r in load_profile("mixed", include_uvloop=True)}
    body = []
    for suffix, label in (("-314", "3.14"), ("-314t", "3.14t")):
        base = by_config.get(f"{UVLOOP_BASE}{suffix}")
        uv = by_config.get(f"{UVLOOP_BASE}-uvloop{suffix}")
        if not base or not uv:
            continue
        delta = (uv["throughput_tps"] - base["throughput_tps"]) / base["throughput_tps"] * 100
        body.append(
            [
                label,
                f"{base['throughput_tps']:.1f}",
                f"{uv['throughput_tps']:.1f}",
                f"{delta:+.1f} %",
            ],
        )
    if not body:
        return "_No uvloop pair measured._"
    return _table(["python", "selector loop TPS", "uvloop TPS", "delta"], ["l", "r", "r", "r"], body)


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
            ],
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
            ],
        )
    return _table(
        ["config", "procs", "RSS modelled", "TPS", f"ideal $/1M @ {rss_mb:.0f} MB"],
        ["l", "r", "r", "r", "r"],
        body,
    )


def crossover_table(profile: str, rss_values: tuple[int, ...] = (100, 800, 1500, 2500, 5000)) -> str:
    """Best prefork config against best aio config as per-process RSS grows."""
    rows = load_profile(profile)
    prefork = [r for r in rows if "-prefork" in r["config"]]
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
        f"- `cpu-only`: 100 % cpu_heavy (`cpu_iters={cpu['cpu_iters']:,}`). Stresses the GIL.".replace(",", " "),
    )
    io = workload.IO_ONLY_DEFAULT
    lines.append(f"- `io-only`: 100 % I/O sleep (`io_seconds={io['io_seconds']}`). Stresses concurrency.")
    return lines


def _git_date() -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--format=%cs"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def render() -> str:
    existing = keeps(RESULTS_MD.read_text()) if RESULTS_MD.exists() else {}
    fw_table, fw_notes = framework_table()
    all_rows = [r for p in PROFILES for r in load_profile(p, include_uvloop=True)]
    counts = {p: len(load_profile(p, include_uvloop=True)) for p in PROFILES}
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
        "Every worker logs its actual event-loop class at startup, so which loop ran is checked rather than assumed.",
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
        window = f"{rows[0]['duration_seconds']:.0f}" if rows else ""
        parts += [
            "",
            f"## Full results, {profile} workload",
            "",
            f"{len(rows)} of {len(MAIN_CONFIGS)} configs, {window} s measured per config"
            f" after a 10 s warmup, sorted by throughput."
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
        "## Memory against concurrency",
        "",
        "Every pool here is sized by how many tasks it can hold in flight, so"
        " memory is only comparable at equal slot counts. `peak RSS` sums the"
        " process tree and therefore charges a prefork pool once per child for"
        " pages its children share; `peak PSS` splits each shared page across"
        " the processes mapping it and is the figure to compare against a"
        " single-process pool.",
        "",
    ]
    for profile in PROFILES:
        parts += [f"### {profile}", "", memory_table(profile), ""]
    parts += [
        "### Observations",
        "",
        keep_block("obs-memory", existing, "_Not written yet for this run._"),
        "",
        "## uvloop",
        "",
        f"`{UVLOOP_BASE}` on the mixed workload, with and without"
        " `BENCH_UVLOOP=1`. This is the whole uvloop result; it is not carried"
        " through the other tables.",
        "",
        uvloop_table(),
        "",
        keep_block("obs-uvloop", existing, "_Not written yet for this run._"),
        "",
        "## Other Python task frameworks",
        "",
        "The mixed workload again, on the free-threaded build, against four other"
        " queues. Every row runs the same task body from `fw_common.py`, the same"
        " Redis, the same 4 pinned cores, results disabled, and counts completions"
        " by having the task bump one Redis key, so the extra round-trip is charged"
        " to every framework equally. Each row is that framework's fastest shape"
        " out of `sweep_fw.sh`.",
        "",
        fw_table,
        "",
        *([f"- {note}" for note in fw_notes] + [""] if fw_notes else []),
        keep_block("obs-frameworks", existing, "_Not written yet for this run._"),
        "",
        "## Where the per-task cost goes",
        "",
        keep_block("obs-profile", existing, "_Not written yet for this run._"),
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
