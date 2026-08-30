#!/usr/bin/env bash
# Short probe runs to pick the fastest config per framework before the real
# measurement. 20 s windows: enough to rank, too short to quote.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
probe() {  # label venv framework slots env...
  local label=$1 venv=$2 fw=$3 slots=$4; shift 4
  env "$@" PYTHONPATH=. taskset -c 8-27 "$venv/bin/python" runner_fw.py \
    --framework "$fw" --config "sweep-$label" --workload "$W" \
    --output "results/sweep-$label.json" --slots "$slots" --python-label "${venv##*-}" \
    --duration 20 --warmup 10 --ready-timeout 90 2>&1 | grep -E "runner_fw|rror" | tail -2
}
# Free-threaded only: the comparison is about thread scaling.
for py in 314t; do
  probe "arq-$py-p4c25"   ".venv-arq-$py"      arq      100 FW_PROCS=4 FW_CONC=25
  probe "arq-$py-p8c12"   ".venv-arq-$py"      arq       96 FW_PROCS=8 FW_CONC=12
  probe "arq-$py-p1c100"  ".venv-arq-$py"      arq      100 FW_PROCS=1 FW_CONC=100
  probe "tkq-$py-p4c25"   ".venv-taskiq-$py"   taskiq   100 FW_PROCS=4 FW_CONC=25
  probe "tkq-$py-p1c100"  ".venv-taskiq-$py"   taskiq   100 FW_PROCS=1 FW_CONC=100
  probe "tkq-$py-p8c12"   ".venv-taskiq-$py"   taskiq    96 FW_PROCS=8 FW_CONC=12
  probe "dmq-$py-p1t100"  ".venv-dramatiq-$py" dramatiq 100 FW_PROCS=1 FW_THREADS=100
  probe "dmq-$py-p4t25"   ".venv-dramatiq-$py" dramatiq 100 FW_PROCS=4 FW_THREADS=25
  probe "dmq-$py-p8t12"   ".venv-dramatiq-$py" dramatiq  96 FW_PROCS=8 FW_THREADS=12
  probe "djq-$py-w4"      ".venv-djangoq-$py"  djangoq    4 FW_PROCS=4
  probe "djq-$py-w16"     ".venv-djangoq-$py"  djangoq   16 FW_PROCS=16
  probe "djq-$py-w32"     ".venv-djangoq-$py"  djangoq   32 FW_PROCS=32
done
probe "cel-aio-314t" .venv-async-314t   celery 101 FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25
probe "cel-thr-314t" .venv-classic-314t celery 100 FW_CELERY_POOL=threads FW_CONC=100
echo "SWEEP DONE"
