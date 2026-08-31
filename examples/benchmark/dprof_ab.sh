#!/usr/bin/env bash
# Same deterministic profiler, both frameworks, so the buckets are comparable.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
one() {  # label venv framework env...
  local label=$1 venv=$2 fw=$3; shift 3
  rm -rf "results/dprof-$label"
  env "$@" BENCH_DPROF=1 BENCH_DPROF_DIR="results/dprof-$label" PYTHONPATH=. taskset -c 8-27 \
    "$venv/bin/python" runner_fw.py --framework "$fw" --config "$label" --workload "$W" \
    --output "results/dp-$label.json" --slots 100 --python-label 3.14t \
    --duration 30 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw|dprof|rror" | tail -3
}
one celery .venv-async-314t  celery FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 FW_PREFETCH=16
one taskiq .venv-taskiq-314t taskiq FW_TASKIQ_BROKER=stream FW_PROCS=4 FW_CONC=25
echo "DPROF_AB_DONE"
