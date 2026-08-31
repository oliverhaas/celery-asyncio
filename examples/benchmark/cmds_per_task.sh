#!/usr/bin/env bash
# Redis commands per task, per framework. taskiq's stream broker hardwires
# result_backend=None, so celery is measured both ways to separate what the
# transport costs from what storing a result costs.
set -u
cd "$(dirname "$0")"
W=results/workload-mixed-10000-s42.json
one() {  # label venv framework env...
  local label=$1 venv=$2 fw=$3; shift 3
  redis-cli config resetstat >/dev/null
  local r0
  r0=$(redis-cli info stats | grep -oP "total_reads_processed:\K\d+")
  local tps
  tps=$(env "$@" PYTHONPATH=. taskset -c 8-27 "$venv/bin/python" runner_fw.py \
    --framework "$fw" --config "$label" --workload "$W" \
    --output "results/cmd-$label.json" --slots 100 --python-label 3.14t \
    --duration 30 --warmup 10 --ready-timeout 120 2>&1 | grep -oP '\d+(?= tasks in)' | tail -1)
  local r1
  r1=$(redis-cli info stats | grep -oP "total_reads_processed:\K\d+")
  echo "===== $label : $tps tasks | client round-trips/task: $(python3 -c "print(f'{($r1-$r0)/$tps:.2f}')") ====="
  redis-cli info commandstats | sed 's/^cmdstat_//' \
    | awk -F'[:,=]' -v n="$tps" '/calls/ {c[$1]=$3; t+=$3}
        END {for (k in c) printf "  %-14s %9d  %6.2f/task\n", k, c[k], c[k]/n;
             printf "  %-14s %9d  %6.2f/task\n", "TOTAL", t, t/n}' \
    | sort -k2 -rn
}
one celery-result   .venv-async-314t  celery FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 FW_PREFETCH=16 BENCH_IGNORE_RESULT=0
one celery-noresult .venv-async-314t  celery FW_CELERY_POOL=asyncio FW_CELERY_ASYNC=1 FW_PROCS=4 FW_CONC=25 FW_SYNC=0 FW_PREFETCH=16 BENCH_IGNORE_RESULT=1
one taskiq-stream   .venv-taskiq-314t taskiq FW_TASKIQ_BROKER=stream FW_PROCS=4 FW_CONC=25
echo "CMDS_DONE"
