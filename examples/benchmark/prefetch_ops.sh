#!/usr/bin/env bash
# Counts what one task costs Redis with batching off and on. evalsha should
# drop from two per task (consume + ack) to about one.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
for pf in 0 16; do
  redis-cli config resetstat > /dev/null
  env FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 \
    FW_PREFETCH="$pf" PYTHONPATH=. taskset -c 8-27 .venv-async-314t/bin/python runner_fw.py \
    --framework celery --config "pfops-$pf" --workload "$W" \
    --output "results/pfops-$pf.json" --slots 100 --python-label 3.14t \
    --duration 30 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw|rror"
  echo "--- prefetch_multiplier=$pf commandstats ---"
  redis-cli info commandstats | sed 's/cmdstat_//;s/:calls=/ /;s/,usec=/ /;s/,.*//' | sort -k2 -nr | head -8
done
echo "PREFETCH_OPS_DONE"
