#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
smoke() {  # label venv framework env...
  local label=$1 venv=$2 fw=$3; shift 3
  echo "--- $label"
  env "$@" PYTHONPATH=. taskset -c 8-27 "$venv/bin/python" runner_fw.py \
    --framework "$fw" --config "smoke-$label" --workload "$W" \
    --output "results/smoke-$label.json" --slots 100 --python-label 314t \
    --duration 10 --warmup 6 --ready-timeout 90 2>&1 | grep -E "runner_fw|rror|Trace" | tail -3
}
smoke celery-aio .venv-async-314t   celery   FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25
smoke celery-thr .venv-classic-314t celery   FW_CELERY_POOL=threads FW_CONC=100
smoke arq        .venv-arq-314t     arq      FW_PROCS=4 FW_CONC=25
smoke taskiq     .venv-taskiq-314t  taskiq   FW_PROCS=4 FW_CONC=25
smoke dramatiq   .venv-dramatiq-314t dramatiq FW_PROCS=1 FW_THREADS=100
smoke djangoq    .venv-djangoq-314t djangoq  FW_PROCS=4
echo "SMOKE DONE"
