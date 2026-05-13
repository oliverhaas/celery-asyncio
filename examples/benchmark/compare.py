"""Print a summary table of all results in results/*.json."""

import argparse
import json
from pathlib import Path

HEADERS = ("config", "variant", "py", "tasks", "wall", "tps", "peak_rss", "mean_rss", "peak_cpu", "mean_cpu")


def _row(r: dict) -> tuple:
    s = r.get("summary", {})
    py = "3.14t" if "freethreaded" in r.get("executable", "") else "3.14"
    return (
        r["config"],
        r["variant"],
        py,
        r["n_tasks"],
        f"{r['complete_seconds']:.1f}s",
        f"{r['throughput_tps']:.0f}",
        f"{s.get('peak_rss_mb', 0):.0f}M",
        f"{s.get('mean_rss_mb', 0):.0f}M",
        f"{s.get('peak_cpu_pct', 0):.0f}%",
        f"{s.get('mean_cpu_pct', 0):.0f}%",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=Path("results"))
    args = ap.parse_args()

    rows = []
    for p in sorted(args.dir.glob("*.json")):
        if p.name.startswith("workload-"):
            continue
        try:
            rows.append(_row(json.loads(p.read_text())))
        except (KeyError, json.JSONDecodeError):
            continue

    if not rows:
        print(f"No result files in {args.dir}/")
        return

    rows.sort(key=lambda r: float(r[4].rstrip("s")))
    widths = [max(len(str(c)) for c in (h, *col)) for h, *col in zip(HEADERS, *rows, strict=True)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*HEADERS))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt.format(*r))


if __name__ == "__main__":
    main()
