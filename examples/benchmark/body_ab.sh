#!/usr/bin/env bash
# Prices the identical task body under each framework. Same function, same
# workload, so any difference is the host, not the work.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
one() {  # label venv framework env...
  local label=$1 venv=$2 fw=$3; shift 3
  echo "===== $label ====="
  env "$@" BENCH_TIME_BODY=1 PYTHONPATH=. taskset -c 8-27 \
    "$venv/bin/python" runner_fw.py --framework "$fw" --config "$label" --workload "$W" \
    --output "results/body-$label.json" --slots 100 --python-label 3.14t \
    --duration 40 --warmup 10 --ready-timeout 120 2>&1 \
    | grep -E "runner_fw|\[body\]" | tail -4
}
for i in 1 2 3 4; do
  one celery .venv-async-314t  celery FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 FW_PREFETCH=16
  one taskiq .venv-taskiq-314t taskiq FW_TASKIQ_BROKER=stream FW_PROCS=4 FW_CONC=25
done
echo "BODY_AB_DONE"
