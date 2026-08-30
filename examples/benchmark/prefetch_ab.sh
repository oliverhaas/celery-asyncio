#!/usr/bin/env bash
# A/B for the batched fetch. worker_prefetch_multiplier=0 leaves the transport
# claiming one message per script call, which is what it did before batching.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
run() {  # label prefetch
  local label=$1 prefetch=$2
  for i in 1 2 3; do
    env FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 \
      FW_PREFETCH="$prefetch" PYTHONPATH=. taskset -c 8-27 \
      .venv-async-314t/bin/python runner_fw.py \
      --framework celery --config "pf-$label" --workload "$W" \
      --output "results/pf-$label-$i.json" --slots 100 --python-label 3.14t \
      --duration 60 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw|rror" | tail -2
  done
}
run off 0
run x4 4
run x16 16
echo "PREFETCH_AB_DONE"
