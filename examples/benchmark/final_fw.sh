#!/usr/bin/env bash
# Final cross-framework rows: every framework's fastest shape from sweep_fw.sh,
# all on the free-threaded build, two runs each.
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
final celery-aio .venv-async-314t    celery   101 FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25
final celery-thr .venv-classic-314t  celery   100 FW_CELERY_POOL=threads FW_CONC=100
final taskiq     .venv-taskiq-314t   taskiq   100 FW_PROCS=4 FW_CONC=25
final dramatiq   .venv-dramatiq-314t dramatiq 100 FW_PROCS=4 FW_THREADS=25
final arq        .venv-arq-314t      arq      100 FW_PROCS=4 FW_CONC=25
final djangoq    .venv-djangoq-314t  djangoq   32 FW_PROCS=32
echo "FINAL DONE"
