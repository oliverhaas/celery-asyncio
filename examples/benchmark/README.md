# Benchmark: celery-asyncio vs classic celery

Apples-to-apples comparison of celery-asyncio against upstream Celery on a
deterministic, mixed CPU/I/O/memory workload. Worker is pinned to four CPUs
via `taskset -c 0,1,2,3` so every config sees a "4-CPU server".

## Task profile

One task with three knobs:

- `cpu_iters` -- inner-loop count of float arithmetic
- `io_seconds` -- sleep (`time.sleep` for sync, `asyncio.sleep` for async)
- `mem_kb` -- bytearray allocated for the task lifetime (touched per page)

Workload is a fixed mix produced by `workload.py` from a seeded RNG:

| profile    | weight | cpu_iters | io     | mem      |
| ---------- | -----: | --------: | -----: | -------: |
| io_heavy   |   40 % |       200 | 50 ms  |   16 KiB |
| cpu_heavy  |   30 % |     4 000 |  0 ms  |   32 KiB |
| mem_heavy  |   20 % |       100 | 10 ms  | 8192 KiB |
| balanced   |   10 % |     1 000 | 20 ms  |  256 KiB |

The exact same workload manifest is fed to every config.

## Configurations

celery-asyncio (Python 3.14 and 3.14t), 4 worker threads each:
- `aio-async-l4c25`     -- async-def task, 4 loop workers x 25 concurrency, 1 sync worker
- `aio-sync-s4`         -- sync-def task, 4 sync worker threads
- `aio-mixed-l2c50-s2`  -- alternating async+sync, 2 loop x 50 + 2 sync (4 worker threads total)

Classic celery (Python 3.14 and 3.14t best-effort):
- `classic-prefork1`  -- `--pool prefork --concurrency 1` (single worker; scaling baseline)
- `classic-prefork4`  -- `--pool prefork --concurrency 4` (4 worker processes)
- `classic-threads4`  -- `--pool threads --concurrency 4` (4 GIL-bound threads)

## Quick start

```bash
# 1. Broker
docker compose up -d

# 2. Venvs (creates .venv-async-314, .venv-async-314t, .venv-classic-314, .venv-classic-314t)
./setup_venvs.sh

# 3. Smoke test
python run_all.py --tasks 500

# 4. Full run
python run_all.py --tasks 10000

# Run a single config only:
python run_all.py --tasks 5000 --only aio-async-314
```

Results land in `results/<config>.json` and a summary table is printed at
the end. Each JSON file has the full per-tick (CPU%, RSS) sample series
under `samples`.

## Measurement

`runner.py` starts the worker in a subprocess, waits for `inspect ping`,
samples the worker process tree's `cpu_percent()` and `memory_info().rss`
every 0.5 s, publishes all tasks at once, and polls `AsyncResult.ready()`
until every task is done. Wall clock is start-of-publish to last-completed.
The sampling thread sums across the worker and all its children (so prefork
multiprocessing is captured).
