"""Deterministic workload generator.

Produces a fixed sequence of `(profile_name, kwargs)` tuples from a seeded
RNG. The same seed + count always produces the same workload, so every
configuration is compared on identical work.

Profiles (weights chosen so the workload exercises all three dimensions):
  - io_heavy    (40%): mostly sleeps. Tests concurrency.
  - cpu_heavy   (30%): mostly arithmetic. Tests parallelism.
  - mem_heavy   (20%): allocates ~8 MiB, light CPU + I/O. Tests RSS growth.
  - balanced    (10%): moderate mix of all three.

Run as a script to dump a workload to a JSON manifest:

    python workload.py --count 10000 --seed 42 --out workload.json
"""

import argparse
import json
import random
from pathlib import Path

PROFILES = {
    "io_heavy": {"weight": 40, "cpu_iters": 200, "io_seconds": 0.050, "mem_kb": 16},
    "cpu_heavy": {"weight": 30, "cpu_iters": 4000, "io_seconds": 0.0, "mem_kb": 32},
    "mem_heavy": {"weight": 20, "cpu_iters": 100, "io_seconds": 0.010, "mem_kb": 8192},
    "balanced": {"weight": 10, "cpu_iters": 1000, "io_seconds": 0.020, "mem_kb": 256},
}


def generate(count: int, seed: int) -> list[tuple[str, dict]]:
    rng = random.Random(seed)
    names = list(PROFILES)
    weights = [PROFILES[n]["weight"] for n in names]
    out: list[tuple[str, dict]] = []
    for _ in range(count):
        name = rng.choices(names, weights=weights, k=1)[0]
        p = PROFILES[name]
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
    ap.add_argument("--out", type=Path, default=Path("workload.json"))
    args = ap.parse_args()

    workload = generate(args.count, args.seed)
    args.out.write_text(
        json.dumps(
            {
                "count": args.count,
                "seed": args.seed,
                "profiles": PROFILES,
                "tasks": [{"profile": p, "kwargs": kw} for p, kw in workload],
            }
        )
    )
    print(f"Wrote {args.count} tasks to {args.out} (seed={args.seed})")


if __name__ == "__main__":
    main()
