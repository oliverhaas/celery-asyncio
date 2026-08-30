#!/usr/bin/env bash
# Full matrix under fixed-duration measurement: every config gets the same
# 60 s window instead of the same task count.
set -u
cd "$(dirname "$0")"
PY=../../.venv/bin/python
for profile in mixed cpu-only io-only; do
  taskset -c 8-27 $PY run_all.py --profile "$profile" --duration 60 --warmup 10 \
    > "results/matrix-$profile.log" 2>&1
  echo "PROFILE DONE: $profile"
done
echo "ALL DONE"
