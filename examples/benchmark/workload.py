"""Deterministic workload generator.

Produces a fixed sequence of `(profile_name, kwargs)` tuples from a seeded
RNG. The same seed + count always produces the same workload, so every
configuration is compared on identical work.

Three workload mixes are available (`--profile`):

  - `mixed`    (default): the balanced default mix that exercises all three
                          dimensions — io_heavy 40%, cpu_heavy 30%,
                          mem_heavy 20%, balanced 10%.
  - `cpu-only`:           100% CPU-bound tasks. Used to showcase the impact
                          of the GIL on multi-threaded configs (compare
                          aio-sync-s4-314 vs aio-sync-s4-314t).
  - `io-only`:            100% I/O-bound tasks. Used to showcase asyncio's
                          concurrency advantage (compare aio-async-l4c25 vs
                          classic-prefork4).

`--cpu-iters` and `--io-seconds` tune the intensity of the cpu-only and
io-only profiles respectively.

Run as a script to dump a workload to a JSON manifest:

    python workload.py --count 10000 --seed 42 --out workload.json
    python workload.py --count 5000 --profile cpu-only --cpu-iters 20000 --out cpu.json
    python workload.py --count 5000 --profile io-only --io-seconds 0.1 --out io.json
"""

import argparse
import json
import random
from pathlib import Path

# Default mixed-workload profiles (used when --profile mixed).
MIXED_PROFILES = {
    "io_heavy": {"weight": 40, "cpu_iters": 200, "io_seconds": 0.050, "mem_kb": 16},
    "cpu_heavy": {"weight": 30, "cpu_iters": 4000, "io_seconds": 0.0, "mem_kb": 32},
    "mem_heavy": {"weight": 20, "cpu_iters": 100, "io_seconds": 0.010, "mem_kb": 8192},
    "balanced": {"weight": 10, "cpu_iters": 1000, "io_seconds": 0.020, "mem_kb": 256},
}

# Single-profile defaults for the showcase modes. cpu-only burns ~5 ms of
# pure-Python compute per task so the GIL effect is visible. io-only sleeps
# 1 s per task — a realistic average for an outbound HTTP API call — so the
# asyncio concurrency advantage isn't drowned out by broker round-trip noise.
CPU_ONLY_DEFAULT = {"cpu_iters": 20000, "io_seconds": 0.0, "mem_kb": 0}
IO_ONLY_DEFAULT = {"cpu_iters": 0, "io_seconds": 0.5, "mem_kb": 0}


def _profiles_for(profile: str, cpu_iters: int | None, io_seconds: float | None) -> dict[str, dict]:
    if profile == "mixed":
        return MIXED_PROFILES
    if profile == "cpu-only":
        p = dict(CPU_ONLY_DEFAULT)
        if cpu_iters is not None:
            p["cpu_iters"] = cpu_iters
        return {"cpu_only": {"weight": 100, **p}}
    if profile == "io-only":
        p = dict(IO_ONLY_DEFAULT)
        if io_seconds is not None:
            p["io_seconds"] = io_seconds
        return {"io_only": {"weight": 100, **p}}
    msg = f"unknown profile: {profile}"
    raise ValueError(msg)


def generate(count: int, seed: int, profiles: dict[str, dict]) -> list[tuple[str, dict]]:
    rng = random.Random(seed)
    names = list(profiles)
    weights = [profiles[n]["weight"] for n in names]
    out: list[tuple[str, dict]] = []
    for _ in range(count):
        name = rng.choices(names, weights=weights, k=1)[0]
        p = profiles[name]
        out.append(
            (
                name,
                {
                    "cpu_iters": p["cpu_iters"],
                    "io_seconds": p["io_seconds"],
                    "mem_kb": p["mem_kb"],
                },
            )
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--profile", choices=("mixed", "cpu-only", "io-only"), default="mixed")
    ap.add_argument("--cpu-iters", type=int, default=None, help="cpu_iters override for cpu-only profile")
    ap.add_argument("--io-seconds", type=float, default=None, help="io_seconds override for io-only profile")
    ap.add_argument("--out", type=Path, default=Path("workload.json"))
    args = ap.parse_args()

    profiles = _profiles_for(args.profile, args.cpu_iters, args.io_seconds)
    workload = generate(args.count, args.seed, profiles)
    args.out.write_text(
        json.dumps(
            {
                "count": args.count,
                "seed": args.seed,
                "profile": args.profile,
                "profiles": profiles,
                "tasks": [{"profile": p, "kwargs": kw} for p, kw in workload],
            }
        )
    )
    print(f"Wrote {args.count} tasks to {args.out} (seed={args.seed}, profile={args.profile})")


if __name__ == "__main__":
    main()
