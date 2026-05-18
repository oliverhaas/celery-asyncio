"""Render RESULTS.md tables from the JSONs in results/.

Loads results/<config>-<profile>.json for every config in run_all.matrix()
and every profile in (mixed, cpu-only, io-only). Emits the markdown tables
identical in shape to the previous RESULTS.md so the file can be assembled
by stitching prose + tables.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Fargate Linux/x86 us-east-1, May 2026.
VCPU_S_RATE = 0.04048 / 3600  # $/vCPU·s
GB_S_RATE = 0.004445 / 3600   # $/GB·s

PROFILES = ("mixed", "cpu-only", "io-only")

# Stable canonical order — TPS will determine sort within tables.
CANONICAL_CONFIGS = [
    "aio-async-l4c25-314", "aio-async-l4c25-314t",
    "aio-async-l4c25-uvloop-314", "aio-async-l4c25-uvloop-314t",
    "aio-sync-s4-314", "aio-sync-s4-314t",
    "aio-sync-s4-uvloop-314", "aio-sync-s4-uvloop-314t",
    "aio-mixed-l2c50-s2-314", "aio-mixed-l2c50-s2-314t",
    "aio-mixed-l2c50-s2-uvloop-314", "aio-mixed-l2c50-s2-uvloop-314t",
    "classic-prefork1-314", "classic-prefork1-314t",
    "classic-prefork4-314", "classic-prefork4-314t",
    "classic-threads4-314", "classic-threads4-314t",
]


def load_profile(profile: str) -> list[dict]:
    slug = profile.replace("-", "_")
    out = []
    for config in CANONICAL_CONFIGS:
        f = ROOT / "results" / f"{config}-{slug}.json"
        if not f.exists():
            continue
        out.append(json.loads(f.read_text()))
    return out


def _fmt_secs(n: float) -> str:
    return f"{n:.1f} s"


def _ideal_cost_per_million(r: dict) -> float:
    """$/1M tasks using mean CPU + mean RSS over the run wall."""
    s = r["summary"]
    cpu_seconds = (s["mean_cpu_pct"] / 100) * r["complete_seconds"]
    mem_gb_seconds = (s["mean_rss_mb"] / 1024) * r["complete_seconds"]
    cost_per_run = cpu_seconds * VCPU_S_RATE + mem_gb_seconds * GB_S_RATE
    return cost_per_run / r["n_completed"] * 1_000_000


def _provisioned_slot(r: dict) -> tuple[float, float]:
    """Return (vCPU, GB) for a Fargate slot sized to peak load + 25% memory headroom."""
    s = r["summary"]
    peak_vcpu = s["peak_cpu_pct"] / 100
    for slot in (0.25, 0.5, 1, 2, 4, 8, 16):
        if peak_vcpu <= slot:
            vcpu = slot
            break
    else:
        vcpu = 16
    mem_gb = s["peak_rss_mb"] * 1.25 / 1024
    # Fargate increments of 0.5 GiB
    mem_slot = math.ceil(mem_gb * 2) / 2
    return vcpu, mem_slot


def _provisioned_cost_per_million(r: dict) -> float:
    vcpu, gb = _provisioned_slot(r)
    cost_per_run = (vcpu * VCPU_S_RATE + gb * GB_S_RATE) * r["complete_seconds"]
    return cost_per_run / r["n_completed"] * 1_000_000


def main_table(profile: str) -> str:
    rows = load_profile(profile)
    rows.sort(key=lambda r: r["complete_seconds"])
    lines = [
        "| config                  | py    | variant | workers              | wall    | TPS    | peak RSS | mean RSS | peak CPU | mean CPU | stranded |",
        "| ----------------------- | ----- | ------- | -------------------- | ------: | -----: | -------: | -------: | -------: | -------: | -------: |",
    ]
    for r in rows:
        s = r["summary"]
        py = "3.14t" if r["config"].endswith("-314t") else "3.14"
        variant = r["variant"]
        workers = _describe_workers(r)
        lines.append(
            "| {cfg:<23} | {py:<5} | {var:<7} | {w:<20} | {wall:>6.1f} s | {tps:>6.1f} | {pr:>5.0f} MB  | {mr:>5.0f} MB  | {pc:>5.0f} % | {mc:>5.0f} % | {st:>6} |".format(
                cfg=r["config"], py=py, var=variant, w=workers,
                wall=r["complete_seconds"], tps=r["throughput_tps"],
                pr=s["peak_rss_mb"], mr=s["mean_rss_mb"],
                pc=s["peak_cpu_pct"], mc=s["mean_cpu_pct"],
                st=r["n_stranded"],
            )
        )
    return "\n".join(lines)


def _describe_workers(r: dict) -> str:
    cfg = r["config"]
    if "prefork1" in cfg:
        return "prefork × 1"
    if "prefork4" in cfg:
        return "prefork × 4"
    if "threads4" in cfg:
        return "threads × 4"
    if "async-l4c25" in cfg:
        return "4 loop × 25 + 1 sync"
    if "sync-s4" in cfg:
        return "4 sync threads"
    if "mixed-l2c50-s2" in cfg:
        return "2 loop × 50 + 2 sync"
    return ""


def cost_table(profile: str) -> str:
    rows = load_profile(profile)
    out = []
    for r in rows:
        s = r["summary"]
        vcpu_s_per_1k = (s["mean_cpu_pct"] / 100) * r["complete_seconds"] / r["n_completed"] * 1000
        mb_s_per_1k = s["mean_rss_mb"] * r["complete_seconds"] / r["n_completed"] * 1000
        ideal = _ideal_cost_per_million(r)
        prov = _provisioned_cost_per_million(r)
        vcpu_slot, gb_slot = _provisioned_slot(r)
        out.append({
            "config": r["config"],
            "tps": r["throughput_tps"],
            "vcpu_s_per_1k": vcpu_s_per_1k,
            "mb_s_per_1k": mb_s_per_1k,
            "ideal_per_m": ideal,
            "prov_per_m": prov,
            "slot": f"{vcpu_slot} vCPU / {gb_slot:g} GB",
        })
    out.sort(key=lambda r: r["ideal_per_m"])
    lines = [
        "| config                  | TPS   | vCPU·s / 1 k | MB·s / 1 k |  ideal $/1M |  prov $/1M | Fargate slot |",
        "| ----------------------- | ----: | -----------: | ---------: | ----------: | ---------: | -----------: |",
    ]
    for x in out:
        lines.append(
            "| {cfg:<23} | {tps:>5.1f} | {vs:>11.2f}  | {ms:>8.0f}   | ${ideal:>7.2f}    | ${prov:>7.2f}   | {slot} |".format(
                cfg=x["config"], tps=x["tps"], vs=x["vcpu_s_per_1k"],
                ms=x["mb_s_per_1k"], ideal=x["ideal_per_m"],
                prov=x["prov_per_m"], slot=x["slot"],
            )
        )
    return "\n".join(lines)


def all_costs() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for profile in PROFILES:
        out[profile] = []
        for r in load_profile(profile):
            s = r["summary"]
            out[profile].append({
                "config": r["config"],
                "tps": r["throughput_tps"],
                "ideal_per_m": _ideal_cost_per_million(r),
                "vcpu_s_per_1k": (s["mean_cpu_pct"] / 100) * r["complete_seconds"] / r["n_completed"] * 1000,
                "mb_s_per_1k": s["mean_rss_mb"] * r["complete_seconds"] / r["n_completed"] * 1000,
            })
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="mixed", choices=PROFILES + ("all",))
    ap.add_argument("--what", default="main", choices=("main", "cost"))
    args = ap.parse_args()
    if args.profile == "all":
        for p in PROFILES:
            print(f"## {p}")
            print(main_table(p) if args.what == "main" else cost_table(p))
            print()
    else:
        print(main_table(args.profile) if args.what == "main" else cost_table(args.profile))
