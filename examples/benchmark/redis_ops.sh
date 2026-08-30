#!/usr/bin/env bash
# Redis commands per task, straight from the server's own counters. The client
# side cannot see batching or pipelining honestly; INFO commandstats can.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
stat() { .venv-async-314t/bin/python -c "
import redis, sys
r = redis.Redis.from_url('redis://localhost:6379/0')
if sys.argv[1] == 'reset':
    r.config_resetstat()
else:
    for k, v in sorted(r.info('commandstats').items(), key=lambda kv: -kv[1]['calls']):
        print(k.replace('cmdstat_', ''), v['calls'], round(v['usec'] / 1000))
" "$1" 2>/dev/null; }

for spec in \
  "celery-aio .venv-async-314t celery FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25" \
  "taskiq .venv-taskiq-314t taskiq FW_TASKIQ_BROKER=stream FW_PROCS=4 FW_CONC=25" \
  "taskiq-list .venv-taskiq-314t taskiq FW_TASKIQ_BROKER=list FW_PROCS=4 FW_CONC=25" \
  "dramatiq .venv-dramatiq-314t dramatiq FW_PROCS=4 FW_THREADS=25"
do
  set -- $spec
  label=$1 venv=$2 fw=$3; shift 3
  stat reset
  env "$@" PYTHONPATH=. taskset -c 8-27 "$venv/bin/python" runner_fw.py \
    --framework "$fw" --config "ops-$label" --workload "$W" \
    --output "results/ops-$label.json" --slots 100 --python-label 3.14t \
    --duration 30 --warmup 10 --ready-timeout 120 2>&1 | grep -E "runner_fw" | tail -1
  echo "--- $label redis commandstats (calls, ms) ---"
  stat show | head -12
done
echo OPS_DONE
