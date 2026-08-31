#!/usr/bin/env bash
# A/B for the client library. The transport dispatches on the URL scheme, so
# valkey:// runs the whole broker and backend path through valkey-py instead of
# redis-py. The bench counter stays on redis-py either way, so the difference is
# celery's own client traffic.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
run() {  # label scheme i
  local label=$1 scheme=$2 i=$3
    env FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 \
      FW_PREFETCH=16 PYTHONPATH=. \
      BENCH_BROKER="$scheme://localhost:6379/0" \
      BENCH_BACKEND="$scheme://localhost:6379/1" \
      BENCH_COUNTER="redis://localhost:6379/2" \
      taskset -c 8-27 \
      .venv-async-314t/bin/python runner_fw.py \
      --framework celery --config "cl-$label" --workload "$W" \
      --output "results/cl-$label-$i.json" --slots 100 --python-label 3.14t \
      --duration 60 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw|rror" | tail -2
}
# Alternating, so slow drift over the half hour hits both arms equally rather
# than landing entirely on whichever one runs second.
for i in 1 2 3 4; do
  run redis redis "$i"
  run valkey valkey "$i"
done
echo "CLIENT_AB_DONE"
