#!/usr/bin/env bash
# Follow-up rows for two fairness questions: does celery gain from 4 separate
# processes the way arq and dramatiq run, and what does taskiq's speed cost once
# its transport acknowledges tasks the way kombu's does.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
final() {  # label venv framework slots env...
  local label=$1 venv=$2 fw=$3 slots=$4; shift 4
  for i in 1 2; do
    env "$@" PYTHONPATH=. taskset -c 8-27 "$venv/bin/python" runner_fw.py \
      --framework "$fw" --config "fw-$label" --workload "$W" \
      --output "results/fw-$label-$i.json" --slots "$slots" --python-label 3.14t \
      --duration 60 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw|rror" | tail -2
  done
}
final celery-aio-p4 .venv-async-314t   celery 100 FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_CELERY_PROCS=4 FW_PROCS=1 FW_CONC=25 FW_SYNC=0
final celery-thr-p4 .venv-classic-314t celery 100 FW_CELERY_POOL=threads FW_CELERY_PROCS=4 FW_CONC=25
final taskiq-stream .venv-taskiq-314t  taskiq 100 FW_TASKIQ_BROKER=stream FW_PROCS=4 FW_CONC=25
echo "EXTRA DONE"
